#!/bin/bash
# 二进制产物端到端验证：启动 -> 登录 -> 建链接 -> 上传(带 SHA-256) -> 校验 -> 下载比对
set -u
cd "$(dirname "$0")/.." 2>/dev/null || true
BIN="${1:-./dist/landrop-server}"
[ -x "$BIN" ] || { echo "找不到可执行文件: $BIN"; exit 1; }

D=$(mktemp -d)
P=$(python3 -c "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1]);s.close()")
"$BIN" --data-dir "$D/data" --host 127.0.0.1 --port "$P" > "$D/log" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null; rm -rf "$D"' EXIT

for _ in $(seq 1 60); do
  curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$P/" && break
  sleep 0.25
done

fail=0
ok(){ echo "  [PASS] $1"; }
no(){ echo "  [FAIL] $1"; fail=$((fail+1)); }

# 管理员密钥：优先从落盘文件读取（唯一可靠来源）
KEY=$(cat "$D/data/admin-key.txt" 2>/dev/null | tr -d '\r\n')
[ -n "$KEY" ] && ok "admin-key.txt 可读到管理员密钥（长度 ${#KEY}）" || no "admin-key.txt 缺失"

echo "[1] 登录"
LOGIN=$(curl -s -c "$D/c.txt" -X POST -H 'Content-Type: application/json' \
        -d "{\"key\":\"$KEY\"}" "http://127.0.0.1:$P/api/login")
echo "$LOGIN" | grep -q '"manage": true' && ok "管理员登录并获得 manage 能力" || no "登录失败: $LOGIN"

echo "[2] 创建仅上传链接"
G=$(curl -s -b "$D/c.txt" -X POST -H 'Content-Type: application/json' \
     -d '{"kind":"share","perm":"upload","label":"bin test"}' "http://127.0.0.1:$P/api/grants")
SECRET=$(echo "$G" | python3 -c "import json,sys;print(json.load(sys.stdin)['grant']['secret'])")
[ -n "$SECRET" ] && ok "链接密钥已生成" || no "创建链接失败: $G"

echo "[3] 访客登录并上传（带本地 SHA-256）"
printf 'binary payload 校验内容\n' > "$D/payload.bin"
head -c 300000 /dev/urandom >> "$D/payload.bin"   # 混入二进制，覆盖非文本字节
SHA=$(sha256sum "$D/payload.bin" | awk '{print $1}')
SIZE=$(stat -c%s "$D/payload.bin")
curl -s -c "$D/v.txt" -X POST -H 'Content-Type: application/json' \
     -d "{\"key\":\"$SECRET\"}" "http://127.0.0.1:$P/api/login" | grep -q '"upload": true' \
  && ok "访客登录（upload 档）" || no "访客登录失败"

UP=$(curl -s -b "$D/v.txt" -X PUT --data-binary "@$D/payload.bin" \
     "http://127.0.0.1:$P/api/upload?name=payload.bin&id=bincheck&offset=0&total=$SIZE&sha256=$SHA")
echo "$UP" | grep -q "\"sha256\": \"$SHA\"" && ok "上传回执 SHA-256 与本地一致" || no "上传回执异常: $UP"
FID=$(echo "$UP" | python3 -c "import json,sys;print(json.load(sys.stdin).get('id',''))")

echo "[4] 服务端复核（从磁盘重算）"
V=$(curl -s -b "$D/c.txt" "http://127.0.0.1:$P/api/verify?id=$FID")
echo "$V" | grep -q '"match": true' && ok "复核一致" || no "复核失败: $V"
echo "$V" | grep -q "\"sha256\": \"$SHA\"" && ok "复核摘要等于原始文件摘要" || no "摘要不符: $V"

echo "[5] 下载回来逐字节比对"
curl -s -b "$D/c.txt" -o "$D/got.bin" "http://127.0.0.1:$P/api/download?id=$FID"
cmp -s "$D/payload.bin" "$D/got.bin" && ok "下载内容与上传前完全一致" || no "下载内容不一致"

echo "[6] 权限：仅上传档不能看列表"
curl -s -b "$D/v.txt" "http://127.0.0.1:$P/api/files" | grep -q '"files": \[\]' \
  && ok "仅上传档列表为空" || no "仅上传档应看不到文件"

echo "[7] 摘要不符必须拒绝"
BAD=$(curl -s -o /dev/null -w '%{http_code}' -b "$D/v.txt" -X PUT --data-binary "@$D/payload.bin" \
      "http://127.0.0.1:$P/api/upload?name=bad.bin&id=badid&offset=0&total=$SIZE&sha256=0000000000000000000000000000000000000000000000000000000000000000")
[ "$BAD" = "422" ] && ok "错误摘要返回 422" || no "错误摘要应返回 422，实际 $BAD"

echo "[8] 重启后数据仍在（同一数据目录）"
kill $PID 2>/dev/null; wait $PID 2>/dev/null
"$BIN" --data-dir "$D/data" --host 127.0.0.1 --port "$P" > "$D/log2" 2>&1 &
PID=$!
for _ in $(seq 1 60); do curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$P/" && break; sleep 0.25; done
curl -s -c "$D/c2.txt" -X POST -H 'Content-Type: application/json' -d "{\"key\":\"$KEY\"}" \
     "http://127.0.0.1:$P/api/login" | grep -q '"manage": true' \
  && ok "重启后管理员密钥仍有效" || no "重启后登录失败"
curl -s -b "$D/c2.txt" "http://127.0.0.1:$P/api/files" | grep -q "$SHA" \
  && ok "重启后文件与摘要记录仍在" || no "重启后记录丢失"
curl -s -b "$D/c2.txt" "http://127.0.0.1:$P/api/grants" | grep -q "$SECRET" \
  && no "列表不应泄露明文密钥" || ok "重启后授权仍在且不泄露明文"

echo "[9] 导入旧目录（复制+校验，含隐藏文件跳过）"
kill $PID 2>/dev/null; wait $PID 2>/dev/null
LEG="$D/legacy"; mkdir -p "$LEG/sub"
echo "keep-content" > "$LEG/keep.txt"
echo "nested" > "$LEG/sub/deep.txt"
touch "$LEG/.gitkeep"
P2=$(python3 -c "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1]);s.close()")
"$BIN" --data-dir "$D/data2" --import "$LEG" --import-recursive --host 127.0.0.1 --port "$P2" > "$D/log3" 2>&1 &
PID=$!
for _ in $(seq 1 60); do curl -s -o /dev/null --max-time 1 "http://127.0.0.1:$P2/" && break; sleep 0.25; done
COUNT=$(python3 - "$D/data2/state.sqlite3" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
rows = list(con.execute("SELECT display_name, sha256, owner_visitor FROM files"))
print(len(rows))
PY
)
echo "$COUNT" | grep -q '^2$' && ok "只导入 2 个普通文件（跳过 .gitkeep）" || no "导入数量异常: $COUNT"
grep -q "导入完成：成功 2" "$D/log3" && ok "导入日志报告成功 2 个" || no "导入日志异常: $(grep 导入 "$D/log3")"
[ -f "$LEG/keep.txt" ] && [ -f "$LEG/.gitkeep" ] && ok "源目录文件未被移动或删除" || no "源目录被改动"
kill $PID 2>/dev/null; wait $PID 2>/dev/null

echo
if [ "$fail" -eq 0 ]; then echo "二进制端到端：全部通过"; else echo "二进制端到端：$fail 项失败"; fi
exit $fail
