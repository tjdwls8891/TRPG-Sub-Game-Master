#!/usr/bin/env bash
# ============================================================
# TRPG 봇 VPS 프로비저닝 스크립트 (Ubuntu/Debian · x86_64·ARM 공용)
#
# 사용법: 레포 루트에서 실행
#   bash deploy/setup.sh
#
# 하는 일:
#   1) 시스템 패키지 설치 (ffmpeg, libopus, python venv 도구)
#   2) .venv 가상환경 생성
#   3) requirements.txt 설치
#   4) .env 존재 여부 확인 안내
# 멱등성: 여러 번 돌려도 안전하다.
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."   # 레포 루트로 이동

echo "[1/4] 시스템 패키지 설치 (ffmpeg · libopus · python3-venv)..."
sudo apt-get update -y
# ffmpeg: 음성 송출 필수 / libopus0: 디스코드 voice 인코딩 / python3-venv,pip: 가상환경
sudo apt-get install -y ffmpeg libopus0 python3-venv python3-pip

echo "[2/4] 파이썬 가상환경 생성 (.venv)..."
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

echo "[3/4] 의존성 설치 (requirements.txt)..."
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "[4/4] .env 확인..."
if [ ! -f .env ]; then
  echo "  ⚠️  .env 가 없습니다. 예시를 복사해 토큰을 채우세요:"
  echo "        cp .env.example .env && nano .env"
  echo "        chmod 600 .env   # 토큰 보호"
else
  echo "  ✅ .env 존재"
fi

echo
echo "✅ 프로비저닝 완료."
echo "   다음 단계(systemd 등록·기동)는 deploy/DEPLOY.md 의 4단계를 참조하세요."
echo "   먼저 수동 구동으로 토큰을 검증하려면:  ./.venv/bin/python main.py"
