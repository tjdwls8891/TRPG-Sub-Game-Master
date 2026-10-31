"""세계 스냅샷 — 버전마다 하나(`world/<VersionId>.json`)와 현재 포인터(`world/current.json`).

되감기 = 포인터를 고른 버전으로 옮기는 것뿐이다. 스냅샷은 한 번 쓰면 바뀌지 않는다
(같은 내용의 재기록은 멱등, 다른 내용은 거부). 보존 개수 제한 없음(결정 K2 가).
"""
import os
import re

from ._store import read_json, write_json
from .errors import KernelStoreError
from .world import World

_VID = re.compile(r"^[A-Za-z0-9_.\-]{1,96}$")


class SnapshotStore:
    def __init__(self, root: str):
        self.root = os.path.join(root, "world")

    def _path(self, vid: str) -> str:
        if not isinstance(vid, str) or not _VID.match(vid):
            raise KernelStoreError(f"잘못된 버전 id {vid!r}")
        return os.path.join(self.root, f"{vid}.json")

    def put(self, world: World) -> str:
        v = world.version
        if not v or not v.get("version_id"):
            raise KernelStoreError("버전 없는 세계는 스냅샷으로 저장하지 않습니다")
        vid = v["version_id"]
        path = self._path(vid)
        old = read_json(path)
        if old is not None:
            if World.from_dict(old).digest() != world.digest():
                raise KernelStoreError(f"같은 버전 {vid}의 다른 스냅샷이 이미 있습니다")
            return vid
        write_json(path, world.to_dict())
        return vid

    def get(self, vid: str) -> World:
        d = read_json(self._path(vid))
        if d is None:
            raise KernelStoreError(f"스냅샷 없음: {vid}")
        w = World.from_dict(d)
        if (w.version or {}).get("version_id") != vid:
            raise KernelStoreError(f"스냅샷 {vid}의 버전이 파일 이름과 다릅니다")
        return w

    def has(self, vid: str) -> bool:
        return os.path.exists(self._path(vid))

    def current_id(self) -> str | None:
        d = read_json(os.path.join(self.root, "current.json"))
        return None if d is None else d.get("version_id")

    def set_current(self, vid: str) -> None:
        if not self.has(vid):
            raise KernelStoreError(f"없는 스냅샷으로 포인터를 옮길 수 없습니다: {vid}")
        write_json(os.path.join(self.root, "current.json"), {"version_id": vid})

    def current(self) -> World | None:
        vid = self.current_id()
        return None if vid is None else self.get(vid)
