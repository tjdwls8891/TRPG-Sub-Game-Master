"""연산 함수 공용 — 인자 키 검사·거부(KernelError → 투영 충돌 HANDLER_REJECTED)."""
from indaim.kernel import KernelError


def reject(effect, msg):
    raise KernelError(f"{effect.id}: {msg}")


def keys(effect, required, optional=()):
    a = effect.args
    extra = set(a) - set(required) - set(optional)
    if extra:
        reject(effect, f"알 수 없는 인자 {sorted(extra)}")
    missing = [k for k in required if k not in a]
    if missing:
        reject(effect, f"인자 누락 {missing}")
    return a


def is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def ident(effect, key):
    v = effect.args[key]
    if not isinstance(v, str) or not v:
        reject(effect, f"{key}는 비어 있지 않은 문자열이어야 합니다")
    return v
