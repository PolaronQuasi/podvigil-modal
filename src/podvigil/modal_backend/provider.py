"""Small, version-pinned Modal SDK boundary. Importing this module is offline.

One logical SDK create per resource. SDK transport retries use its own idempotency keys;
an exception never starts another logical create. Unknown responses stay unknown.
"""

from importlib.metadata import version
from pathlib import Path
import asyncio
import hashlib
import hmac
import io
import json
import math
import time

from . import SDK_VERSION
from .errors import UserError, LifecycleError, CreateNotSent


def bounded_create(create, allowance, *args, **kwargs):
    """Cancel SDK scheduling at the remaining allowance; still require independent exit reads."""

    async def invoke():
        return await asyncio.wait_for(create.aio(*args, **kwargs), timeout=allowance)

    return asyncio.run(invoke())


def sdk():
    try:
        installed = version("modal")
    except Exception:
        raise LifecycleError(
            "Install podvigil-modal with its pinned dependencies before using cloud commands."
        ) from None
    if installed != SDK_VERSION:
        raise LifecycleError("This backend requires Modal SDK " + SDK_VERSION + ".")
    import modal

    return modal


def credential_proof(salt):
    sdk()
    from modal.config import config

    values = [config.get("token_id"), config.get("token_secret"), config.get("server_url")]
    if not all(values):
        raise LifecycleError(
            "Configure Modal's token locally before launch; credentials are never uploaded."
        )
    endpoints = values[2].split(",")
    if not endpoints or any(
        endpoint not in ("https://api.modal.com", "https://api.modal2.com")
        for endpoint in endpoints
    ):
        raise LifecycleError("Only Modal's official API endpoint is allowed.")
    value = json.dumps(values).encode()
    return {"modal": hmac.new(salt.encode(), value, hashlib.sha256).hexdigest()}


class Provider:
    def __init__(self, spec):
        self.modal = sdk()
        self.spec = spec
        self._volumes = {}
        self._sandboxes = {}

    def prepare(self):
        self.app = self.modal.App.lookup(
            self.spec["app"], environment_name=self.spec["environment"], create_if_missing=True
        )
        # Only the runner file is attached. Never upload an entire checkout or an environment.
        self.image = self.modal.Image.from_registry(self.spec["image"]).add_local_file(
            Path(__file__).with_name("runtime.py"), "/podvigil/runtime.py"
        )

    def create_volume(self, name, *, acknowledged=lambda: None):
        self.modal.Volume.objects.create(
            name, version=2, allow_existing=False, environment_name=self.spec["environment"]
        )
        acknowledged()  # Persist the successful create reply even if the following lookup fails.
        for attempt in range(3):
            try:
                return self.lookup_volume(name)
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)  # Read retries only; never another create.

    def lookup_volume(self, name):
        volume = self.modal.Volume.from_name(
            name, environment_name=self.spec["environment"], create_if_missing=False
        )
        volume.hydrate()
        return volume.object_id

    def resolve_volume(self, job):
        """Repeat the acknowledged create's exact read, never create or infer absence."""
        try:
            return self.lookup_volume(job["volume"]["name"])
        except self.modal.exception.NotFoundError:
            return None

    def build_image(self, allowance):
        if allowance <= 0:
            raise LifecycleError("Startup allowance expired before image preparation.")
        self.image = bounded_create(self.image.build, allowance, app=self.app)

    def volume(self, volume_id):
        if volume_id in self._volumes:
            return self._volumes[volume_id]
        volume = self.modal.Volume.from_id(volume_id)
        volume.hydrate()
        self._volumes[volume_id] = volume
        return volume

    def upload(self, volume_id, files):
        with self.volume(volume_id).batch_upload(force=False) as batch:
            for name, value in files.items():
                batch.put_file(io.BytesIO(value) if isinstance(value, bytes) else value, "/" + name)

    def create_sandbox(self, job, remaining):
        spec = self.spec
        try:
            volume = self.volume(job["volume"]["id"])
        except Exception:
            raise CreateNotSent(
                "Sandbox creation was not sent because its Volume could not be loaded. No compute was created by this attempt."
            ) from None
        timeout = min(86400, math.floor(job["deadline"] - time.time()))
        if timeout <= 0:
            raise CreateNotSent(
                "Sandbox creation was not sent because no whole second remains before the fixed deadline."
            )
        sandbox = bounded_create(
            self.modal.Sandbox.create,
            remaining,
            "python3",
            "/podvigil/runtime.py",
            "/podvigil-data/run.json",
            app=self.app,
            name=job["sandbox"]["name"],
            tags={"podvigil_job": job["job_id"], "podvigil_owner": job["owner"]},
            image=self.image,
            timeout=timeout,
            gpu=spec.get("gpu"),
            cpu=(spec["cpu"], spec["cpu"]),
            memory=(spec["memory_mib"], spec["memory_mib"]),
            volumes={"/podvigil-data": volume},
            secrets=[],
            include_oidc_identity_token=False,
            block_network=spec["block_network"],
        )
        return sandbox.object_id

    def runtime_info(self, job):
        """Read committed supervisor metadata, never infer a start from a create acknowledgement."""
        data = bytearray()
        try:
            for chunk in self.read_file(job["volume"]["id"], "runtime-start.json"):
                data.extend(chunk)
                if len(data) > 65536:
                    raise LifecycleError("Runtime start metadata exceeds its limit.")
        except (self.modal.exception.NotFoundError, FileNotFoundError):
            return None
        receipt = json.loads(data)
        if receipt.get("job_id") != job["job_id"] or receipt.get("owner") != job["owner"]:
            raise LifecycleError("Runtime start ownership does not match this job.")
        if any(
            type(receipt.get(key)) not in (int, float) or not math.isfinite(receipt[key])
            for key in ("started_at", "run_deadline")
        ):
            raise LifecycleError("Runtime start timing is invalid.")
        if (
            receipt["run_deadline"] < receipt["started_at"]
            or receipt["run_deadline"] > job["deadline"]
        ):
            raise LifecycleError("Runtime start exceeds the fixed deadline.")
        return receipt

    def resolve_sandbox(self, job):
        """Reconcile only this intent's exact name, environment and two unpredictable tags."""
        try:
            sandbox = self.modal.Sandbox.from_name(
                self.spec["app"],
                job["sandbox"]["name"],
                environment_name=self.spec["environment"],
                include_terminated=True,
            )
        except self.modal.exception.NotFoundError:
            # include_terminated=True does not take the SDK's older GPU lookup fallback.
            # Retry only this read, with the public running-Sandbox route that does.
            try:
                sandbox = self.modal.Sandbox.from_name(
                    self.spec["app"],
                    job["sandbox"]["name"],
                    environment_name=self.spec["environment"],
                    include_terminated=False,
                )
            except self.modal.exception.NotFoundError:
                return None  # A lookup miss still does not prove that nothing was created.
        tags = sandbox.get_tags()
        if tags.get("podvigil_job") != job["job_id"] or tags.get("podvigil_owner") != job["owner"]:
            raise LifecycleError("Sandbox ownership tags do not match its creation receipt.")
        return sandbox.object_id

    def sandbox(self, job, *, recheck=False):
        key = (job["sandbox"]["id"], job["job_id"], job["owner"])
        sandbox = self._sandboxes.get(key)
        if sandbox is None:
            sandbox = self.modal.Sandbox.from_id(key[0])
            recheck = True
        if recheck:
            tags = sandbox.get_tags()
            if (
                tags.get("podvigil_job") != job["job_id"]
                or tags.get("podvigil_owner") != job["owner"]
            ):
                raise LifecycleError("Sandbox ownership tags do not match its creation receipt.")
            self._sandboxes[key] = sandbox
        return sandbox

    def poll(self, job):
        return self.sandbox(job).poll()

    def log_tail(self, job, allowance=5):
        # A fixed, bounded reader runs in the already-owned container. Never execute log text.
        code = """import json, os, stat
from pathlib import Path
root = Path('/podvigil-data')
owner = json.loads((root / 'owner.json').read_text())
assert owner == OWNER
result = {}
for name in ('stdout', 'stderr'):
    path = root / 'outputs' / (name + '.log')
    result[name] = ''
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        continue
    with os.fdopen(fd, 'rb') as stream:
        assert stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
        stream.seek(max(0, os.fstat(stream.fileno()).st_size - 32768))
        result[name] = stream.read(32768).decode('utf-8', errors='replace')
print(json.dumps(result))
""".replace("OWNER", repr({"job_id": job["job_id"], "owner": job["owner"]}))

        async def collect():
            # Hydration and fresh ownership reads share the stream's timeout. Synchronous
            # SDK lookups here would block the control loop outside that allowance.
            key = (job["sandbox"]["id"], job["job_id"], job["owner"])
            sandbox = self._sandboxes.get(key)
            if sandbox is None:
                sandbox = await self.modal.Sandbox.from_id.aio(key[0])
            tags = await sandbox.get_tags.aio()
            if (
                tags.get("podvigil_job") != job["job_id"]
                or tags.get("podvigil_owner") != job["owner"]
            ):
                raise LifecycleError("Sandbox ownership tags do not match its creation receipt.")
            self._sandboxes[key] = sandbox
            process = await sandbox.exec.aio("python3", "-c", code, timeout=5)
            output = await process.stdout.read.aio()
            if await process.wait.aio() != 0:
                raise LifecycleError("Workload log snapshot could not be read.")
            if len(output) > 512 * 1024:
                raise LifecycleError("Workload log snapshot exceeds its limit.")
            return json.loads(output)

        async def bounded():
            return await asyncio.wait_for(collect(), timeout=min(5, allowance))

        return asyncio.run(bounded())

    def terminate(self, job):
        self.sandbox(job, recheck=True).terminate()

    def request_stop(self, job):
        sandbox = self.sandbox(job, recheck=True)
        code = (
            "import json,os,signal;from pathlib import Path;r=json.loads(Path('/tmp/podvigil-supervisor.json').read_text());assert r['job_id']=="
            + repr(job["job_id"])
            + " and r['owner']=="
            + repr(job["owner"])
            + ";os.kill(r['pid'],signal.SIGTERM)"
        )
        process = sandbox.exec("python3", "-c", code, timeout=10)
        if process.wait() != 0:
            raise LifecycleError(
                "The supervisor could not be signalled; hard termination remains the fallback."
            )

    def files(self, volume_id):
        from modal.volume import FileEntryType

        files = {}
        for entry in self.volume(volume_id).iterdir("/", recursive=True):
            if entry.type == FileEntryType.DIRECTORY:
                continue
            if entry.type != FileEntryType.FILE:
                raise LifecycleError("Volume contains a link or unsupported file type.")
            name = entry.path.removeprefix("/")
            if name in files:
                raise LifecycleError("Duplicate path in Volume inventory.")
            files[name] = entry.size
        return files

    def read_file(self, volume_id, path):
        yield from self.volume(volume_id).read_file(path)

    def volume_exists(self, volume_id):
        try:
            self.modal.Volume.from_id(volume_id).hydrate()  # Absence checks must bypass the cache.
            return True
        except self.modal.exception.NotFoundError:
            return False

    def delete_volume(self, volume_id):
        # Public objects.delete(name) resolves a name AGAIN just before deleting. A rename/reuse
        # race could delete another Volume. This pinned SDK seam deletes the receipted ID itself.
        self.volume(volume_id)._instance_delete()
        self._volumes.pop(volume_id, None)
