# 세계 정의 식별자 — 정규 직렬화·안정 digest·저작 ID 파생 (H01)
#
# [왜 필요한가]
#   새 세계 정의(인물·정보·장소·이동 구간)는 표시 이름이 아니라 안정 ID로
#   서로를 참조한다(DESIGN 01 §2). ID와 정의 revision이 프로세스·재시작·
#   dict 순서·PYTHONHASHSEED에 따라 달라지면 복구와 재서술이 다른 세계를 본다.
#   그래서 직렬화와 digest를 한 곳에 모은다.
#
# [규칙]
#   · 정규 직렬화는 키 정렬 + 고정 구분자 + NaN/Infinity 금지 + 문자열 키만.
#   · digest 입력은 (네임스페이스, 부분들)의 정규 직렬화다. 문자열 이어붙이기로
#     만들지 않는다 — "a:b"+"c" 와 "a"+"b:c" 가 충돌한다.
#   · 접두사(npc_·info_·edge_)는 디버깅용 표기일 뿐 API 계약이 아니다.
#     코드는 접두사를 해석하지 않는다(DESIGN 01 §3.1).
#   · 이 모듈은 시간·난수·전역 상태를 읽지 않는다. 같은 입력 → 같은 출력.
import hashlib
import json
import re

# digest 길이(hex 문자 수). 24자 = 96비트 — 한 세션의 정의 카탈로그 안에서
# 충돌을 걱정할 필요가 없는 크기이면서 사람이 로그에서 대조할 수 있는 길이.
ID_DIGEST_HEX = 24

# 저작자가 직접 적는 명시 ID의 허용 형식. 공백·경로 구분자를 막아
# 파일명·로그·키로 그대로 써도 안전하게 한다.
EXPLICIT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$")


class CanonicalSerializationError(ValueError):
    """정규 직렬화할 수 없는 값(비문자열 키·NaN·지원하지 않는 타입)."""


def _check(obj, path: str = "$"):
    """정규 직렬화 가능한 JSON 값인지 재귀 검사한다.

    json.dumps 는 int 키를 문자열로 조용히 바꿔 {1: x, "1": y} 를 충돌시키고,
    set 은 순서가 없어 결정성을 깬다. 둘 다 여기서 거부한다.
    """
    if obj is None or isinstance(obj, (bool, str)):
        return
    if isinstance(obj, int):
        return
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            raise CanonicalSerializationError(f"{path}: NaN/Infinity는 직렬화할 수 없습니다")
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise CanonicalSerializationError(
                    f"{path}: 문자열이 아닌 키 {k!r}({type(k).__name__})")
            _check(v, f"{path}.{k}")
        return
    if isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _check(v, f"{path}[{i}]")
        return
    raise CanonicalSerializationError(f"{path}: 지원하지 않는 타입 {type(obj).__name__}")


def canonical_json(obj) -> str:
    """정규 JSON 문자열. 키 정렬·고정 구분자·UTF-8 원문(ensure_ascii=False)."""
    _check(obj)
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def canonical_bytes(obj) -> bytes:
    return canonical_json(obj).encode("utf-8")


def ordered_json(obj) -> str:
    """삽입 순서를 보존한 고정 형식 JSON. 순서 지문(order digest) 전용.

    런타임 일부(장소 순회·NPC 전개 순서)는 dict 순서에 의존한다. 정규 직렬화는
    순서를 지우므로, 순서까지 정의의 일부로 고정할 때만 이것을 쓴다.
    """
    _check(obj)
    return json.dumps(obj, sort_keys=False, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def sha256_hex(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def content_digest(obj) -> str:
    """정규 직렬화 SHA-256. 'sha256:<hex>' 형식(알고리즘을 값에 남긴다)."""
    return "sha256:" + sha256_hex(canonical_bytes(obj))


def order_digest(obj) -> str:
    """삽입 순서 보존 직렬화 SHA-256. 'sha256:<hex>' 형식."""
    return "sha256:" + sha256_hex(ordered_json(obj).encode("utf-8"))


def stable_digest(namespace: str, *parts, length: int = ID_DIGEST_HEX) -> str:
    """(네임스페이스, 부분들)의 결정적 digest(hex 앞 length자).

    네임스페이스에 용도와 규칙 버전을 넣는다(예: "actor.authored_npc.v1").
    규칙이 바뀌면 네임스페이스 버전을 올려 옛 ID와 섞이지 않게 한다.
    """
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("namespace는 비어 있지 않은 문자열이어야 합니다")
    return sha256_hex(canonical_bytes([namespace, list(parts)]))[:length]


def is_valid_explicit_id(value) -> bool:
    return isinstance(value, str) and bool(EXPLICIT_ID_PATTERN.match(value))


# ── 저작 ID 파생 ──────────────────────────────────────────────
# DESIGN 01 §7.3: 명시 ID가 있으면 쓰고, 없으면 시나리오 id + 사전 키로 결정적 생성.
# DESIGN 11 §13: persistent authored identity = scenario + structural source key.

def authored_npc_actor_id(scenario_id: str, npc_key: str, explicit_id=None) -> str:
    """저작 NPC의 ActorId. explicit_id 유효성 검사는 컴파일러가 진단으로 보고한다.

    표시 이름(name) 변경은 ID를 바꾸지 않는다. 그러나 명시 ID가 없으면
    default_npcs 사전 키(또는 시나리오 id)를 바꾸는 것은 ID 변경 = ID migration이다.
    """
    if explicit_id is not None and is_valid_explicit_id(explicit_id):
        return explicit_id
    return "npc_" + stable_digest("actor.authored_npc.v1", scenario_id, npc_key)


def pc_actor_id(uid) -> str:
    """PC의 ActorId. Discord user id가 이미 안정 계정 정체성이다(DESIGN 01 §7.2)."""
    s = str(uid).strip()
    if not s.isdigit():
        raise ValueError(f"PC uid는 숫자 문자열이어야 합니다: {uid!r}")
    return "pc_" + s


def npc_inline_secret_info_id(actor_id: str) -> str:
    """NPC '비밀' 필드(저작 sugar)가 컴파일되는 InfoId. ActorId에 묶여 개명과 무관하다."""
    return "info_" + stable_digest("info.npc_inline_secret.v1", actor_id)


def quest_hidden_info_id(scenario_id: str, quest_id: str) -> str:
    """퀘스트 hidden.truth(저작 sugar)가 컴파일되는 InfoId.

    정체성 입력은 퀘스트 id뿐이다. version·hidden 본문은 메타데이터라 바뀌어도
    InfoId가 유지된다(현행 런타임 quest._find_quest 도 id만으로 찾는다).
    """
    return "info_" + stable_digest("info.quest_hidden.v2", scenario_id, quest_id)


def movement_edge_pair_id(scenario_id: str, from_place: str, to_place: str) -> str:
    """명시 id 없는 저작 이동 구간의 기본 id.

    목록 위치를 넣지 않는다 — 저작 순서를 바꿨다고 구간 정체성이 바뀌면 안 된다.
    같은 (from, to)를 명시 id 없이 두 번 적으면 컴파일러가 ERROR로 막는다.
    """
    return "edge_" + stable_digest("edge.authored.v1", scenario_id, from_place, to_place)
