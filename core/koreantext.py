# 한국어 텍스트 조립 — 슬롯 치환과 조사 보정
#
# [왜 별도 모듈인가]
#   시작 상황 틀·퀘스트 가이드·인트로 지시문이 모두 '빈칸을 채운 뒤
#   조사를 맞추는' 같은 작업을 한다. 각자 구현하면 한쪽만 고쳐지고
#   다른 쪽에는 어색한 문장이 남는다.
#
# [치환 위치에만 적용하는 이유]
#   텍스트 전체를 훑어 교정하면 동사 활용형을 조사로 오인한다.
#   '말라붙은' → '말이라붙은' 같은 손상이 실제로 발생했다(4.38.1).
#   따라서 슬롯 값이 끝난 자리의 조사 하나만 검사한다.
import re

# 받침 유무로 갈리는 조사 쌍. (받침 있을 때, 없을 때)
JOSA_PAIRS = [
    ("이", "가"),
    ("을", "를"),
    ("은", "는"),
    ("과", "와"),
    ("으로", "로"),
    ("이었", "였"),
    ("이라", "라"),
    ("이며", "며"),
    ("이나", "나"),
    ("이란", "란"),
    ("으로써", "로써"),
    ("으로서", "로서"),
]

# 'ㄹ' 받침은 '으로/로'에서 예외다. '칼로', '물로'처럼 '로'를 쓴다.
RIEUL_EXCEPTIONS = {"으로", "로", "으로써", "로써", "으로서", "로서"}

HANGUL_START = 0xAC00
HANGUL_END = 0xD7A3


def has_batchim(ch: str) -> bool:
    """한글 음절에 받침이 있는지."""
    if not ch:
        return False
    code = ord(ch)
    if not (HANGUL_START <= code <= HANGUL_END):
        return False
    return (code - HANGUL_START) % 28 != 0


def is_rieul(ch: str) -> bool:
    """받침이 'ㄹ'인지. '으로/로' 판정에 쓴다."""
    if not ch:
        return False
    code = ord(ch)
    if not (HANGUL_START <= code <= HANGUL_END):
        return False
    return (code - HANGUL_START) % 28 == 8   # 종성 인덱스 8 = ㄹ


def pick_josa(word: str, with_batchim: str, without_batchim: str) -> str:
    """단어에 맞는 조사를 고른다.

    Args:
        word: 조사가 붙을 단어
        with_batchim: 받침이 있을 때의 형태 (예: '이')
        without_batchim: 받침이 없을 때의 형태 (예: '가')
    """
    if not word:
        return without_batchim
    last = word[-1]
    # 한글이 아니면(숫자·영문) 받침 없음으로 처리한다.
    if not (HANGUL_START <= ord(last) <= HANGUL_END):
        return without_batchim
    # 'ㄹ' 받침은 '으로' 계열에서 받침 없는 형태를 쓴다.
    if with_batchim in RIEUL_EXCEPTIONS and is_rieul(last):
        return without_batchim
    return with_batchim if has_batchim(last) else without_batchim


def attach(word: str, josa: str) -> str:
    """단어에 조사를 붙인다. 어느 형태를 적어도 알아서 맞춘다.

    >>> attach("금속음", "가")
    '금속음이'
    >>> attach("정적", "가")
    '정적이'
    """
    for a, b in sorted(JOSA_PAIRS, key=lambda x: -len(x[0])):
        if josa in (a, b):
            return word + pick_josa(word, a, b)
    return word + josa


def fix_josa_at(text: str, pos: int) -> str:
    """지정 위치 직후의 조사 하나만 보정한다.

    pos는 치환된 슬롯 값의 끝 인덱스다.
    """
    if not text or pos <= 0 or pos >= len(text):
        return text
    prev = text[pos - 1]
    if not (HANGUL_START <= ord(prev) <= HANGUL_END):
        return text

    for with_b, without_b in sorted(JOSA_PAIRS, key=lambda x: -len(x[0])):
        for cand in (with_b, without_b):
            if text.startswith(cand, pos):
                correct = pick_josa(prev, with_b, without_b)
                if cand != correct:
                    return text[:pos] + correct + text[pos + len(cand):]
                return text
    return text


def substitute(text: str, slots: dict, *, joiner: str = ", ") -> str:
    """슬롯을 치환하고 그 자리의 조사만 보정한다.

    틀에는 아무 조사나 적어두면 되고, 무엇이 들어오든 자동으로 맞춰진다.
    치환하지 않은 본문은 손대지 않는다.

    Args:
        joiner: 슬롯 값이 리스트일 때 이어붙일 구분자
    """
    if not text:
        return ""
    out = text
    for key, val in (slots or {}).items():
        token = "{" + key + "}"
        if isinstance(val, (list, tuple)):
            value = joiner.join(str(x) for x in val)
        else:
            value = str(val)
        while True:
            idx = out.find(token)
            if idx < 0:
                break
            out = out[:idx] + value + out[idx + len(token):]
            out = fix_josa_at(out, idx + len(value))
    return out


def strip_unfilled(text: str) -> str:
    """채워지지 않은 슬롯 표기를 제거한다.

    슬롯 정의가 빠졌을 때 '{장소}'가 그대로 노출되는 것을 막는다.
    """
    return re.sub(r"\{[^}]{1,20}\}", "", text or "").replace("  ", " ").strip()
