# media — 기능 명세

- **모듈**: `core/audio_mixer.py`(393) · `tts.py`(141) · `tts_preset.py`(256) · `media.py`(149) · `media_control.py`(153)
- **연관 cogs**: `cogs/media.py`(732줄)
- **기준 버전**: v5.30.0
- **작성 상태**: 완료 (미검증 — 사용자 확인 대기)

> ⚠️ **환경 의존 위험 1건이 있습니다.** `audioop`이 Python 3.13에서 제거됩니다.

---

## 개요

소리와 그림을 다룬다.

| 모듈 | 담당 |
|---|---|
| `audio_mixer.py` | BGM 위에 효과음을 겹쳐 단일 스트림으로 |
| `tts.py` | Gemini TTS → 48kHz stereo PCM |
| `tts_preset.py` | 시스템 문구 사전 합성 (런타임 API 0회) |
| `media.py` | 이미지 키워드 전송, 플레이리스트 |
| `media_control.py` | 미디어 토글, 상황 기반 BGM 선택 |

---

## 설계 의도

### 믹서가 필요한 이유

> *"discord.py의 VoiceClient는 동시에 단 하나의 AudioSource만 재생한다. 따라서 BGM이 흐르는 도중 효과음을 '겹쳐' 내보내려면, 봇이 직접 두 PCM 스트림을 20ms 프레임 단위로 합산(mix)하여 하나의 소스로 만들어 재생해야 한다."* `[코드]`

```
VoiceClient.play(MixerAudioSource)  ← 단 한 번
  base    현재 BGM 또는 플리 트랙. set_base()로 교체
  effects 활성 효과음들. add_effect()로 추가, 소진 시 자동 제거
매 read()마다 audioop.add로 합산
```

**영속성** — *"base도 effects도 없을 때는 무음 프레임을 반환해 보이스 연결을 유지한다."* `[코드]`

### 효과음 PCM 캐시

> *"매 재생마다 ffmpeg 프로세스를 새로 띄우는 비용(30~100ms)을 제거한다."* `[코드]`

`_PCM_CACHE[(path, mtime)] = bytes`

### TTS 사전 합성

시스템 문구는 매번 같다. 미리 만들어 두면 **런타임 API 호출이 0회**다.
`!tts생성`으로 일괄 생성한다.

### BGM은 상황이 바뀔 때만 전환

> *"기획 규정 — 상황이 변하지 않으면 재생을 유지하고, 전환할 때만 새 트랙을 반환한다."* `[코드]`
> *"매 턴 트랙이 바뀌면 몰입이 끊긴다."* `[코드]`

`last_bgm_situation = (tag, band)`가 같으면 `None`을 반환한다.
**같은 트랙이 다시 뽑혀도 유지**한다 — 전환 의미가 없기 때문.

---

## 데이터 구조

### 오디오 상수

| 상수 | 값 | 의미 |
|---|---|---|
| `FRAME_SIZE` | 3840 | 48kHz × 20ms × 2ch × 2byte |
| `SAMPLE_WIDTH` | 2 | 16-bit |
| `SILENCE` | 0×3840 | 무음 프레임 |
| `SFX_DIR` | `media/_sfx` | 효과음 |
| `PRESET_DIR` | `media/_tts_preset` | 사전 합성 |

### 미디어 플래그 (`DEFAULT_MEDIA_FLAGS`)

`tts` · `image` · `bgm` · `sfx` 4종. 디스플레이 버튼으로 토글한다.

`sync_tts_flag`가 `session.tts_enabled`와 플래그를 동기화한다.

### 긴장도 구간

```
TENSION_LOW / MID / HIGH
tension_band(session, tension) → 구간 판정
```

임계값은 `extraction.get_thresholds`의 `tension_high`(71)를 쓴다. `[추정]`

### 시나리오 JSON

```json
"bgm_map": {
  "전투": {"high": ["battle_a", "battle_b"], "mid": ["tense_a"]},
  "대화": {"low": ["calm_a"]},
  "_default": {...}
}
"media_keywords": {"강철수": "강철수.png", "영도지도": "영도지도.png"}
```

**폴백 2단계** `[코드]`
```
① 태그 미등록 → bgm_map["_default"]
② 구간에 트랙 없음 → band → MID → LOW → HIGH 순 완화 탐색
```

### 파일 경로

```
media/{시나리오명}/       인물·장소 이미지, BGM 트랙
media/_sfx/               효과음 (dice 등)
media/_tts_preset/        사전 합성 PCM + index.json
```

**폴더명은 시나리오 id와 같아야 한다.** 5.28.1에서 이것 때문에 49개 이미지가 전부 안 나갔다.

---

## 기능 목록

### `audio_mixer.py`

#### `_pad_frame(frame) -> bytes`

프레임을 정확히 `FRAME_SIZE`로 맞춘다(짧으면 0 패딩, 길면 절단).
**호출부** — 내부 4곳

#### `decode_to_pcm(path) -> bytes`

**무엇을 하는가** — ffmpeg으로 파일을 48kHz stereo PCM으로 디코드한다. 캐시한다.

**호출부** — `audio_mixer.py` 내부 3곳. **모듈 밖에서는 부르지 않는다.**

#### `PCMBytesAudioSource` (클래스)

바이트 배열을 `AudioSource`로 감싼다. 효과음용.

#### `MixerAudioSource` (클래스)

**핵심 클래스.**
```
_base           BGM/플리 (PCMVolumeTransformer)
_base_paused    일시정지 플래그
_base_lock      교체 보호 (threading.Lock)
_effects        효과음 목록
_effect_queue   thread-safe 추가 큐
```

> *"effect 추가는 thread-safe 큐로, base 교체는 lock으로 보호한다."* `[코드]`

`read()`가 20ms마다 불린다. base + effects를 `audioop.add`로 합산.

#### `get_mixer(vc)` / `ensure_mixer(bot, vc)`

기존 믹서 조회 · 없으면 생성 후 `vc.play`.
**호출부** — 15곳 / 3곳

#### `active_volume_source(vc)`

볼륨 조절 대상 소스.
**호출부** — 6곳

#### `preload_sfx(name="dice")` / `play_sfx_on_vc(vc, name, volume)` / `play_dice_sfx(bot, guild, delay)`

효과음 사전 디코드 · 재생 · 주사위 효과음.
**호출부** — `main.py:152` / `audio_mixer.py:393` / `ui.py:190,274` · `character.py:126`

---

### `tts.py`

#### `clean_text_for_tts(text) -> str`

마크다운·마커를 제거해 읽을 텍스트만 남긴다.
**호출부** — `tts.py:81` · `game.py:659,915` 등 5곳

#### `synthesize_tts_pcm(bot, text, voice_name=None) -> tuple`

**반환** — `(pcm, cost, in_tokens, out_tokens)`

Gemini native TTS를 호출하고 믹서용 PCM으로 변환한다.
**호출부** — `tts_preset.py:176` · `game.py:879,922`

---

### `tts_preset.py`

| 함수 | 하는 일 | 호출부 |
|---|---|---|
| `load_index()` | `index.json` | 내부 2곳 |
| `get(key, voice)` | 사전 합성 PCM | `tts_preset.py:150` |
| `has(key, voice)` | 존재 여부 | `tts_preset.py:150` |
| `text_of(key)` | 문구 조회 | ⚠️ **호출부 없음** |
| `collect_targets()` | 생성 대상 목록 | 내부 2곳 |
| `needs_build()` | 미생성 항목 | 내부 2곳 |
| `build(bot, ...)` | 일괄 생성 | `system.py:735` (`!tts생성`) |
| `play(vc, key, voice)` | 재생 | `audio_mixer.py:326` |
| `stats()` | 생성 현황 | `system.py:711,719` |

> ⚠️ **`index.json`이 없다.** 사전 합성이 한 번도 실행되지 않았다.
> `!tts생성`을 돌리지 않으면 `play`가 항상 실패하고 시스템 문구는 무음이다.

---

### `media.py`

#### `send_image_by_keyword(game_channel, master_ctx, session, keyword)`

**무엇을 하는가** — 키워드에 대응하는 이미지를 전송한다.

**경로** — `media/{scenario_id}/{media_keywords[keyword]}`
장소는 `places.image_for`로 상속 이미지를 찾는다.

**호출부** — **9곳** (`game.py` 다수)

#### `PlaylistManager` (클래스)

플레이리스트 순회 재생.
**호출부** — `media.py:570`

---

### `media_control.py`

| 함수 | 하는 일 | 호출부 |
|---|---|---|
| `get_media_flags(session)` | 플래그 조회 | `display.py:224` 등 4곳 |
| `set_media_flag(session, key, value)` | 토글 | `display.py:229` |
| `is_enabled(session, key)` | 활성 여부 | **8곳** |
| `sync_tts_flag(session, value)` | `tts_enabled` 동기화 | `display.py:227` |
| `tension_band(session, tension)` | 구간 판정 | `media_control.py:107` |
| `select_bgm(session, situation)` | 트랙 선택 | `gm.py:3287` |
| `describe_bgm_pending(session)` | 켠 직후 안내 | `display.py:233` |
| `format_flags(session)` | 표기 | `display.py:126` |

---

## 흐름

### 턴 중 미디어

```
묘사 출력 (dialogue.stream_text_to_channel)
  @대사:이름| 마커 → maybe_send_speaker_image
  키워드 감지 → send_image_by_keyword
  ↓
추출층위 → situation {tag, tension}
  ↓
select_bgm(session, situation)
  is_enabled("bgm") 확인
  tension_band로 구간 판정
  last_bgm_situation과 같으면 None (유지)
  bgm_map에서 트랙 선택
  ↓ 트랙이 나오면
ensure_mixer(bot, vc).set_base(새 트랙)
```

### TTS 더빙

```
tts_enabled 확인 (game.py:663)
  ↓
_synthesize_and_enqueue(session, texts, voice_name)
  force 아니면 tts_enabled 재확인 (5.26.0 방어)
  get_mixer(vc) 없으면 no_voice
  각 문단마다
    clean_text_for_tts
    synthesize_tts_pcm → (pcm, cost, in, out)
    mixer.add_effect(PCMBytesAudioSource(pcm))
```

### 효과음

```
주사위 굴림 (ui.py)
  play_dice_sfx(bot, guild)
    get_mixer(vc)
    decode_to_pcm(media/_sfx/dice.mp3)  ← 캐시됨
    mixer.add_effect(...)
```

---

## 다른 영역과의 접점

| 상대 | 방향 | 내용 |
|---|---|---|
| **ai** | ai → media | `dialogue`가 `maybe_send_speaker_image` · `send_image_by_keyword` |
| **world** | media → world | `places.image_for` · `resolve` |
| **cost** | media → cost | `calculate_image_gen_cost` · `accrue` |
| **ui** | ui → media | `display`가 `get_media_flags` · `format_flags` · `describe_bgm_pending` |
| **base** | media → base | `write_cost_log` · `save_session_data` |

---

## 발견 사항

> ⚠️ **`audioop`이 Python 3.13에서 제거된다**
> ```
> DeprecationWarning: 'audioop' is deprecated and slated for removal in Python 3.13
> ```
> 코드가 이미 대비를 적어 두었다.
> > *"이전 시 numpy(int32 합산 후 클립) 등으로 `_mix_frames`만 교체하면 된다."* `[코드]`
>
> 운영 서버가 3.12이므로 당장 문제는 없으나, **파이썬을 올리면 음성이 전부 멈춘다.**

> ⚠️ **TTS 사전 합성이 실행된 적이 없다**
> `media/_tts_preset/index.json`이 없다.
> `!tts생성`을 돌리지 않으면 `play`가 항상 실패하고 시스템 문구는 무음이다.
> **런타임 API 0회라는 설계 이점이 실현되지 않았다.**

> ⚠️ **`tts_preset.text_of` 호출부 없음**
> 문구 조회 함수. `PRESET_MESSAGES`를 직접 참조하는 방식으로 대체된 듯하다.

> ⚠️ **`irregular_npc.voice_for`가 연결되지 않아 인물별 목소리가 없다**
> (world 영역에서 지적) `synthesize_tts_pcm(voice_name=None)`으로만 불려
> 전부 기본 나레이터 보이스다.

> ⚠️ **BGM 트랙 파일이 하나도 없다**
> ```
> 영도 bgm_map      9개 태그
> 등록 트랙          2종 (긴장감 · 잔잔)
> 실제 파일          0개
> ```
> `select_bgm`이 트랙 이름을 반환해도 재생할 파일이 없다.
> `bgm_map`은 저작됐는데 **오디오 자산이 준비되지 않았다.**

> ⚠️ **효과음은 `dice.mp3` 하나뿐**
> `media/_sfx/`에 주사위 효과음만 있다.

> 🔁 **`get`·`has`·`build` 같은 일반 이름**
> `tts_preset.get`은 전수 추적 시 `dict.get`과 섞여 1,212건이 잡힌다.
> 실제 동작에는 문제없으나 조사가 어렵다.

---

## 확인 필요 목록

### 환경

- [ ] **`audioop` 제거 대비를 지금 해야 합니까?** Python 3.13으로 올리면
      음성이 전부 멈춥니다. 코드에 *"numpy로 `_mix_frames`만 교체"*라는
      메모가 있습니다.

### 자산

- [ ] **`!tts생성`을 실행한 적이 있습니까?** `index.json`이 없어
      사전 합성 이점(런타임 API 0회)이 실현되지 않은 상태입니다.
- [ ] **BGM 트랙 파일이 하나도 없습니다.** `bgm_map`은 9개 태그·2종 트랙이
      저작됐는데 `media/영도/`에 해당 파일이 없습니다.
      오디오 자산을 준비할 계획이 있습니까?
- [ ] `irregular_npc.voice_for`를 TTS에 연결해야 합니까? (world 영역과 중복)

### 정리

- [ ] `tts_preset.text_of` 호출부가 없습니다. 제거해도 됩니까?
