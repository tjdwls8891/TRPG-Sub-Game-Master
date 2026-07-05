# 24/7 배포 가이드 — 소형 VPS + systemd (무료 우선)

이 봇을 외부 서버에서 상시 구동하기 위한 가이드. **무료로 영구 운영 가능한 Oracle Cloud
Always Free(ARM)** 를 기준으로 작성했으며, 다른 리눅스 VPS에도 그대로 적용된다.

> 왜 이 방식인가 — 이 봇은 ① Discord 게이트웨이 지속 연결(서버리스 불가), ② `ffmpeg`/
> `libopus` 시스템 패키지 필요, ③ `sessions/`·`media/`·`scenarios/` 를 **로컬 디스크에 계속
> 기록**(영속 디스크 필요), ④ 단일 인스턴스만 허용(토큰 중복 실행 금지)이라는 제약을 가진다.
> VPS + systemd 가 이 네 제약과 정확히 일치한다.

---

## 0. 사양

| 항목 | 최소 | 권장 |
|---|---|---|
| OS | Ubuntu 22.04 / 24.04 (Debian 계열) | 동일 |
| CPU | 1 vCPU | 1~2 vCPU (음성 믹싱 사용 시) |
| RAM | 512 MB | 1 GB |
| 디스크 | 10 GB | 20 GB+ (미디어 누적 대비) |
| 아키텍처 | x86_64 / **ARM(aarch64) 모두 가능** | — |

ARM(Oracle Ampere, 라즈베리파이) 에서도 `ffmpeg`·`PyNaCl`·`pillow`·`google-genai` 모두
aarch64 휠/패키지가 있어 그대로 동작한다.

---

## 1. 무료 VPS 생성 — Oracle Cloud Always Free

1. <https://www.oracle.com/cloud/free/> 가입 (신용카드 인증 필요하나 Always Free 자원은 과금 안 됨).
2. **Compute → Instances → Create Instance**
   - Image: **Ubuntu 22.04 (또는 24.04)**
   - Shape: **Ampere A1 (ARM)** — Always Free 한도 내(최대 4 OCPU / 24 GB)에서 1 OCPU·6 GB 정도면 충분
   - SSH 키: 로컬 공개키 등록 (없으면 콘솔에서 키쌍 생성 후 개인키 저장)
3. **인바운드 포트 개방 불필요** — 이 봇은 아웃바운드만 사용한다(웹훅 아님). 기본 보안 그룹 그대로 둔다.

> 대안 무료: **GCP e2-micro Always Free**(미국 리전, x86), AWS 프리티어(12개월 한정).
> Oracle ARM 용량이 부족하면("out of capacity") 리전을 바꾸거나 잠시 후 재시도한다.
> Oracle 무료 인스턴스는 **장기 유휴 시 회수될 수 있으니** 봇을 실제로 돌리는 한 안전하다.

---

## 2. 접속 & 코드 배치

```bash
ssh ubuntu@<서버_공인IP>

# 레포 가져오기 (예: ~/trpg-bot 에 배치 — systemd 유닛 기본 경로와 일치)
git clone <당신의_레포_URL> ~/trpg-bot
cd ~/trpg-bot
```

> ⚠️ **미커밋 자산 주의** — `git clone` 은 **커밋된 파일만** 가져온다. 현재 작업트리에서
> 아직 커밋되지 않은 `media/` 이미지·`scenarios/*.json` 이 있다면(있음), 먼저 커밋해 푸시하거나
> 아래처럼 직접 복사한다:
> ```bash
> # 로컬(맥)에서 실행 — 미디어/시나리오 통째 전송
> rsync -avz --exclude sessions ./media ./scenarios ubuntu@<서버IP>:~/trpg-bot/
> ```
> 기존 진행 중인 **세션을 이어가려면** 로컬 `sessions/` 도 함께 복사한다(`sessions/` 는
> `.gitignore` 대상이라 clone 에 포함되지 않는다).

---

## 3. 프로비저닝 & 환경변수

```bash
cd ~/trpg-bot
bash deploy/setup.sh          # ffmpeg·libopus·venv·의존성 자동 설치

cp .env.example .env
nano .env                     # DISCORD_TOKEN, GEMINI_API_KEY, TRPG_INTRO_TEXT 채우기
chmod 600 .env                # 토큰 파일 권한 잠금
```

토큰 검증을 위해 **먼저 수동으로 한 번** 띄워본다(Ctrl+C 로 종료):

```bash
./.venv/bin/python main.py
# "봇 로그인 완료" 류 메시지가 뜨고 디스코드에서 온라인이면 OK
```

---

## 4. systemd 서비스 등록 & 기동

`deploy/trpg-bot.service` 의 **3줄(User·WorkingDirectory·ExecStart)** 이 본인 경로와 맞는지
확인한다(기본값은 `ubuntu` 계정 + `~/trpg-bot`). 다르면 수정 후:

```bash
sudo cp ~/trpg-bot/deploy/trpg-bot.service /etc/systemd/system/trpg-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now trpg-bot      # 부팅 시 자동 시작 + 지금 즉시 기동

sudo systemctl status trpg-bot            # 상태 확인 (active (running) 이면 성공)
```

이제 서버 재부팅·봇 크래시 시 systemd 가 **자동으로 다시 띄운다**(`Restart=always`).

---

## 5. 운영 (로그·재시작)

```bash
# 실시간 로그
journalctl -u trpg-bot -f

# 최근 200줄
journalctl -u trpg-bot -n 200 --no-pager

# 재시작 / 정지 / 시작
sudo systemctl restart trpg-bot
sudo systemctl stop trpg-bot
sudo systemctl start trpg-bot

# 재시작 폭주로 멈췄을 때(5분 5회 초과) 카운터 리셋
sudo systemctl reset-failed trpg-bot
```

---

## 6. 업데이트 워크플로

```bash
cd ~/trpg-bot
git pull
# 의존성이 바뀌었으면:
./.venv/bin/pip install -r requirements.txt
sudo systemctl restart trpg-bot
```

- **`cogs/` 만 고쳤다면** 봇을 안 내리고 마스터 채널에서 `!리로드 [모듈명]` 으로 핫스왑 가능
  (`game`·`character`·`media`·`session`·`system`·`auto_gm`).
- **`core/`·`main.py`·`prompts.py` 변경은 핫스왑 불가** → 위처럼 `systemctl restart` 필요.
- `prompts.py` 의 `SYSTEM_INSTRUCTION` 을 바꿨으면 재시작 후 활성 세션에서 `!캐시 재발급`.

---

## 7. 주의사항 (Gotchas)

- **단일 인스턴스 원칙** — 같은 `DISCORD_TOKEN` 으로 봇을 두 곳에서 동시에 띄우면 게이트웨이가
  충돌한다. 로컬에서 테스트 중이라면 서버 봇을 멈추거나 **별도 테스트용 토큰**을 쓴다.
- **상태 백업** — `sessions/`(진행 데이터·로그)와 `media/`·`scenarios/`(생성 이미지·`!이미지
  생성`이 덮어쓴 JSON)는 VPS 디스크에만 있다. VPS 자체는 영속이지만, 인스턴스 사고에 대비해
  주기적으로 받아두면 안전하다:
  ```bash
  # 로컬에서: 서버 → 내 PC 로 백업
  rsync -avz ubuntu@<서버IP>:~/trpg-bot/sessions ./backup/
  ```
- **`.env` 보안** — `chmod 600 .env`. 절대 커밋 금지(`.gitignore` 에 이미 포함).
- **시간대(선택)** — 비용 계산은 `time.time()`(UTC epoch)이라 TZ 와 무관하나, 로그 타임스탬프를
  KST 로 보고 싶으면 `sudo timedatectl set-timezone Asia/Seoul`.
- **빌드 도구(드묾)** — ARM 에서 일부 휠이 없어 pip 가 소스 빌드를 시도하면
  `sudo apt-get install -y build-essential python3-dev` 후 재설치.
- **디스크 모니터링** — 미디어/이미지가 누적된다. `df -h` 로 가끔 확인.

---

## 부록: 한눈에 보는 전체 시퀀스

```bash
# 서버에서
git clone <레포URL> ~/trpg-bot && cd ~/trpg-bot
bash deploy/setup.sh
cp .env.example .env && nano .env && chmod 600 .env
./.venv/bin/python main.py        # 수동 검증 후 Ctrl+C
sudo cp deploy/trpg-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now trpg-bot
journalctl -u trpg-bot -f         # 가동 확인
```
