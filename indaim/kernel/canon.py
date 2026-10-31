"""정규 직렬화와 결정적 digest. 옛 `core.world_ids`와 바이트 단위로 같은 결과를 낸다(id 연속성)."""
import hashlib
import json

ID_DIGEST_HEX = 24


class CanonicalError(ValueError):
    """정규 직렬화할 수 없는 값(비문자열 키·NaN·지원하지 않는 타입)."""


def _check(obj, path="$"):
    if obj is None or isinstance(obj, (bool, str, int)):
        return
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            raise CanonicalError(f"{path}: NaN/Infinity는 직렬화할 수 없습니다")
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise CanonicalError(f"{path}: 문자열이 아닌 키 {k!r}({type(k).__name__})")
            _check(v, f"{path}.{k}")
        return
    if isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _check(v, f"{path}[{i}]")
        return
    raise CanonicalError(f"{path}: 지원하지 않는 타입 {type(obj).__name__}")


def canonical_json(obj) -> str:
    _check(obj)
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_digest(obj) -> str:
    return "sha256:" + _sha(canonical_json(obj).encode("utf-8"))


def stable_digest(namespace: str, *parts, length: int = ID_DIGEST_HEX) -> str:
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("namespace는 비어 있지 않은 문자열이어야 합니다")
    return _sha(canonical_json([namespace, list(parts)]).encode("utf-8"))[:length]
