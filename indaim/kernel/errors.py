class KernelError(RuntimeError):
    """커널 계약 위반(호출자 버그) — 형식·등록·동결 규칙."""


class KernelStoreError(RuntimeError):
    """저장소 손상·영속 실패 — 추정하지 않고 표면화한다."""
