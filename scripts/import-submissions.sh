#!/usr/bin/env bash
# GC_AUTO_TA 的纯 bash 兜底版：不装 Python 也能把「学生目录 → Gitea 仓库」这一步跑完。
# 适合应急（Python 环境坏掉、只想补推某几个学生）时用。
#
# 前置：先让 Python 版把 code/<学生>/ 准备好（或你手工放好），再跑本脚本。
#
# 用法：
#   export GITEA_URL="http://localhost:3000"
#   export GITEA_TOKEN="<PAT>"          # 只在本地环境变量里，不要提交
#   export GITEA_OWNER="your-gitea-username"             # PAT 所属用户名
#   export ORG="JOJ"
#   export ASSIGNMENT="hw1"
#   export PUBLIC_REPO="true"           # true=公开仓库（workflow 匿名 clone）；
#                                       # false=私有 → 先给组织建 secret GRADE_TOKEN（值=同一个 PAT，加 read:repository）
#   export CONF_DIR="./course-conf/hw1" # 评分文件目录（conf.json / run_tests.m / expected.txt）
#   ./scripts/import-submissions.sh ./work/hw1/code
#
# 与 Python 版的差异：
#   * 不做解包、不做名单匹配（假定输入已经是「一目录一学生」）
#   * 不写状态库（重复运行会重复推送，但仓库内容一致，评测会再跑一次）
set -euo pipefail

: "${GITEA_URL:?请设置 GITEA_URL}"
: "${GITEA_TOKEN:?请设置 GITEA_TOKEN}"
: "${GITEA_OWNER:?请设置 GITEA_OWNER}"
: "${ORG:?请设置 ORG}"
: "${ASSIGNMENT:?请设置 ASSIGNMENT}"
SUBMISSIONS_DIR="${1:-./work/$ASSIGNMENT/code}"
CONF_DIR="${CONF_DIR:-./course-conf/$ASSIGNMENT}"
PUBLIC_REPO="${PUBLIC_REPO:-true}"
BRANCH="${BRANCH:-main}"
RUNNER_LABEL="${RUNNER_LABEL:-golang-ubuntu-latest}"
CONF_ROOT="${CONF_ROOT:-joj}"
CONF_NAME="${CONF_NAME:-conf.json}"

api() { curl -sf -H "Authorization: token $GITEA_TOKEN" -H "Content-Type: application/json" "$@"; }

for dir in "$SUBMISSIONS_DIR"/*/; do
  student="$(basename "$dir")"
  repo="${ASSIGNMENT}-${student}"
  echo "==> $student → $ORG/$repo"

  # 1) 建仓（已存在则忽略错误）
  api -X POST -d "{\"name\":\"$repo\",\"private\":$([ "$PUBLIC_REPO" = true ] && echo false || echo true),\"default_branch\":\"$BRANCH\"}" \
      "$GITEA_URL/api/v1/orgs/$ORG/repos" >/dev/null || echo "   （仓库可能已存在，继续）"

  # 2) 组装工作区：评分文件 + 学生代码 + workflow
  work="$(mktemp -d)"
  mkdir -p "$work/$CONF_ROOT" "$work/.gitea/workflows"
  cp -r "$CONF_DIR"/. "$work/$CONF_ROOT"/
  cp -r "$dir"/. "$work"/ 2>/dev/null || true
  printf '* text=auto eol=lf\n' > "$work/.gitattributes"

  # 私有仓库必须带 token clone：容器里匿名 401，且无终端，git 卡在 could not read Username。
  # $D 是字面量 $，用来在 heredoc 里拼出 ${{ secrets.GRADE_TOKEN }}（直接写会被 bash 当坏参数展开）。
  D='$'
  if [ "$PUBLIC_REPO" = true ]; then
    clone_cmd="git clone -q $GITEA_URL/$ORG/$repo.git repo"
  else
    clone_cmd="git clone -q \"${GITEA_URL%%://*}://$GITEA_OWNER:$D{{ secrets.GRADE_TOKEN }}@${GITEA_URL#*://}/$ORG/$repo.git\" repo"
  fi

  cat > "$work/.gitea/workflows/joj3.yaml" <<YAML
name: joj3
on: [push, workflow_dispatch]
jobs:
  grade:
    runs-on: $RUNNER_LABEL
    steps:
      - name: get code
        run: $clone_cmd
      - name: grade
        working-directory: repo
        run: joj3 -conf-root $CONF_ROOT -conf-name $CONF_NAME
      - name: show result
        working-directory: repo
        run: cat joj3_result.json
YAML

  # 3) 推送（凭据走临时 askpass，不出现在命令行里）
  askpass="$(mktemp)"
  cat > "$askpass" <<'SH'
#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' "$GC_GIT_USER" ;;
  *Password*) printf '%s\n' "$GC_GIT_PASS" ;;
  *) printf '\n' ;;
esac
SH
  chmod +x "$askpass"
  export GC_GIT_USER="$GITEA_OWNER" GC_GIT_PASS="$GITEA_TOKEN" GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0

  git -C "$work" init -q -b "$BRANCH" 2>/dev/null || { git -C "$work" init -q; git -C "$work" checkout -q -b "$BRANCH"; }
  git -C "$work" add -A
  git -C "$work" -c user.email=ta@local -c user.name=TA commit -q -m "feat($ASSIGNMENT): $student submission [$ASSIGNMENT]"
  git -C "$work" -c "core.askPass=$askpass" -c credential.helper= push -q "$GITEA_URL/$ORG/$repo.git" "HEAD:refs/heads/$BRANCH"

  rm -rf "$work" "$askpass"
  echo "   已推送"
done

echo "全部导入完成；评测由 workflow 自动触发。收分可用：python3 -m gc_auto_ta grade -c config.yaml"
