#!/data/data/com.termux/files/usr/bin/bash
# 크림 감시 루프. 10분 ± 60초 간격 (일정한 박자를 피해 차단 위험을 낮춘다).
termux-wake-lock
while true; do
  {
    echo "=== $(date '+%F %T')"
    proot-distro login ubuntu -- bash -c 'cd /root/refurb-watch && (git pull -q || true) && NTFY_TOPIC=$(cat /root/.ntfy_topic) /root/venv/bin/python kream.py'
  } >> ~/refurb.log 2>&1
  tail -n 400 ~/refurb.log > ~/refurb.log.tmp && mv ~/refurb.log.tmp ~/refurb.log
  sleep $((600 + RANDOM % 120 - 60))
done
