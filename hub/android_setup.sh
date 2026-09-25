#!/data/data/com.termux/files/usr/bin/bash
# 안드로이드 허브 설치 (Termux 안에서 실행)
#   curl -fsSLo setup.sh https://raw.githubusercontent.com/Kimsclub85/refurb-watch/main/hub/android_setup.sh && bash setup.sh <ntfy토픽>
#   (curl | bash 로 파이프하면 pkg가 stdin을 먹어 스크립트가 잘릴 수 있어서 파일로 받는다)
# 하는 일: Termux 안에 Ubuntu(proot) 설치 → 저장소 clone → curl_cffi 설치 → 10분 루프 시작 → 재부팅 자동 시작 등록
set -e
TOPIC="$1"
if [ -z "$TOPIC" ]; then echo "사용법: bash android_setup.sh <ntfy토픽>"; exit 1; fi

pkg update -y
pkg install -y proot-distro curl
proot-distro install ubuntu 2>/dev/null || echo "ubuntu 이미 설치됨"

proot-distro login ubuntu -- bash -c "
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv git ca-certificates tzdata >/dev/null
  if [ -d /root/refurb-watch ]; then cd /root/refurb-watch && git pull -q; else git clone -q https://github.com/Kimsclub85/refurb-watch /root/refurb-watch; fi
  [ -x /root/venv/bin/python ] || python3 -m venv /root/venv
  /root/venv/bin/pip install -q --upgrade pip curl_cffi
  printf '%s' '$TOPIC' > /root/.ntfy_topic
  echo '--- 첫 실행 (감시 시작 알림이 폰에 와야 정상)'
  cd /root/refurb-watch && NTFY_TOPIC=\$(cat /root/.ntfy_topic) /root/venv/bin/python kream.py
"

curl -fsSL https://raw.githubusercontent.com/Kimsclub85/refurb-watch/main/hub/refurb_loop.sh -o ~/refurb_loop.sh
chmod +x ~/refurb_loop.sh
mkdir -p ~/.termux/boot
printf '#!/data/data/com.termux/files/usr/bin/bash\ntermux-wake-lock\nnohup ~/refurb_loop.sh >/dev/null 2>&1 &\n' > ~/.termux/boot/start-refurb.sh
chmod +x ~/.termux/boot/start-refurb.sh

pkill -f refurb_loop.sh 2>/dev/null || true
termux-wake-lock
nohup ~/refurb_loop.sh >/dev/null 2>&1 &
echo
echo "완료. 루프 가동 중 (10분 간격). 로그: tail -f ~/refurb.log"
echo "남은 것: 설정 > 앱 > Termux > 배터리 > '제한 없음'. 재부팅 자동 시작은 F-Droid에서 Termux:Boot 설치 후 한 번 실행."
