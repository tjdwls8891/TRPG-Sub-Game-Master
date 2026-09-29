# WP-G 제어 동기화 제안 (Control Sync Proposal)

> **상태: IMPLEMENTED / AWAITING FINAL GATE.** 이 문서는 제안입니다. 아래 동기화는 **독립 GPT 최종 게이트가 PASS를 낸 뒤에만** 적용합니다. 구현자는 WP-G나 7-WP 프로그램을 VERIFIED로 선언하지 않습니다.

## 0. 후보 식별

| 항목 | 값 |
|---|---|
| start SHA | `c508ce76afef3c66182d879a18e91fb501a1ae0e` (WP-F VERIFIED) |
| 코드 후보 SHA | `003fff1d82bbee30a6abe9356e2af3393f7eb132` (코드·테스트·문서·매트릭스) |
| 최종 후보 SHA | 이 제안서·완료 번들·스캔을 담은 커밋(부모 = `003fff1`). 정확한 값은 push 직후 채팅 보고 |
| 브랜치 | `claude/wp-g-legacy-retirement-final-stabilization` |
| 최종 회귀 | `631 passed, 0 failed, 0 xfailed, 0 XPASS` |
| 사용자 결정 | D1(L1 방향·종류별 권위) · D2(a 유지, b 제거, c/d/e 유지) · D3(a) · U-2(a) · U-3(a 유지) — 2026-09-29 |

## 1. `CURRENT_PROGRAM_CHECKPOINT.md` (PASS 후 교체안)

```text
Control date: <PASS 날짜>
Latest independently VERIFIED SHA: <최종 후보 SHA>
Verified regression frontier: 631 passed, 0 failed, 0 xfailed, 0 XPASS
Current package: NONE — 7-WP refactor program COMPLETE (WP-A..WP-G VERIFIED)
Next architectural WP: NONE (there is no WP-H)
```

§1 verified chain에 추가합니다:
`7. WP-G — Legacy Retirement, Semantic Reconciliation & Final Stabilization`

§2 runtime map에 추가합니다:
- 공유 묘사 엔진 `_execute_proceed` 호출자 = 자동 턴(`_dispatch_proceed`) + 인트로(`play_intro`). 수동 `!진행`·`!수정`은 은퇴했습니다.
- 현재 `!재생성` = WP-E `rerender_latest` 단일 위임.
- 자동 GM 비용 상한 = CostLedger 세션 provider 비용(활성화 기준 스냅샷, fail-closed). 레거시 `total_cost`/`total_usd`/`turn_cost_log`는 비권위 미러입니다.
- 압축 provider 비용 = 운영자 부담 유지비(선결제 개념 없음).
- 운영자 계정 도구(`!지급`·`!잉크`)·가입선물 = strict 계정 쓰기.
- 프롬프트 권위 = D1 종류별 권위(CLAUDE.md "프롬프트 권위" 절).

## 2. `AUDIT_STATE.json` (PASS 후 교체안)

```json
{
  "control_date": "<PASS 날짜>",
  "mode": "program_complete",
  "latest_verified_checkpoint": {
    "package": "WP-G — Legacy Retirement, Semantic Reconciliation & Final Stabilization",
    "commit_sha": "<최종 후보 SHA>",
    "regression": "631 passed, 0 failed, 0 xfailed, 0 XPASS",
    "status": "VERIFIED_CLOSED"
  },
  "next_authorized_package": null,
  "program_status": "7WP_COMPLETE",
  "final_audit_range": "AUD-001..AUD-063",
  "final_audit_summary": {
    "RESOLVED_IN_CODE": 52,
    "INTENTIONALLY_RETAINED_WITH_RATIONALE": 11,
    "RETRACTED": 0,
    "NEEDS_INTENT": 0
  },
  "user_decisions": {
    "D1": "kind-based authority (L1 direction, not a linear chain)",
    "D2": {"a": "retain", "b": "remove placeholders", "c": "retain+document", "d": "retain", "e": "retain"},
    "D3": "compression = operator-borne; prepayment retired",
    "U2": "mark every applied compression",
    "U3": "CostLedger session provider cost cap (all hints), fail-closed"
  }
}
```

**이중 최신 ID 드리프트 방지(AUD-052):** 최신 SHA는 `latest_verified_checkpoint.commit_sha` 한 곳에만 둡니다. 다른 필드는 이 값을 참조하도록 제안합니다.

## 3. `AUDIT_LEDGER.md`

- AUD-001..063 각 행에 WP-G 최종 상태와 증거 링크(`handoff/WP_G_FINAL_AUDIT_MATRIX.md`의 같은 ID)를 **추가**합니다. 과거 서술은 수정·삭제하지 않습니다(append-only).
- 요약 카운트를 위 `final_audit_summary`와 일치시킵니다(AUD-041 재발 방지).
- WP-G 발견 사실을 기록합니다: 인계서의 "AUD-053 is likely resolved"는 사실이 아니었고 WP-G에서 코드로 해소했습니다.

## 4. `TRPG_7WP_MASTER_PLAN_CURRENT.md`

- §15 WP-G 상태: `IMPLEMENTED / AWAITING FINAL GATE` → PASS 후 `VERIFIED_CLOSED`.
- 프로그램 상태: `COMPLETE — no WP-H`.

## 5. `DOCUMENT_AUTHORITY_INDEX.md` — 문서 분류

| 문서 | 분류 |
|---|---|
| `CLAUDE.md` (저장소) | CURRENT — 런타임·권위·검증 루틴(명령어 44, SESSION_FIELDS 82) |
| `handoff/WP_G_*` | CURRENT(최종 후보 증거) → PASS 후 HISTORICAL |
| `handoff/WP00_*`, `WP02_*`, `WP_A..F_*`, `WP_*_01_*` | HISTORICAL |
| `specs/*` (v5.29.1 역추출) | HISTORICAL — `specs/00_INDEX.md` 머리에 표기함 |
| `SPEC_RULES.md` | HISTORICAL(명세 작업 규칙) |
| `CLAUDE.md` "기능 명세 작업" 절 | HISTORICAL로 표기함(WP 지시와 상충하던 "코드 수정 금지" 문구) |
| `DEVLOG.md` | HISTORICAL 로그(수치는 `verify_docs`가 동기화) |
| 패킷 `WP_G_MASTER_HANDOFF.md` 등 | PASS 후 SUPERSEDED |

## 6. 저장소 문서의 남은 권고(범위 밖, 결정 불요)

- **버전 번호:** `__version__`은 v5.33.0 그대로입니다. WP-A~G 동안 올리지 않았습니다. 프로그램 종료 후 MINOR 이상(프롬프트 변경 포함)으로 올릴지는 운영 배포 시 사용자가 결정합니다. 올리면 `verify_docs --fix`가 문서 수치를 맞춥니다.
- **배포 시:** prompts.py와 `scenarios/무협.json`이 바뀌었으므로 운영 반영 후 `!캐시 재발급`이 필요합니다(CLAUDE.md 배포 절 규칙).
