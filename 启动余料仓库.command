#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if [[ -d '余料仓库.app' ]]; then
  open './余料仓库.app'
else
  print '尚未生成桌面应用。请在本目录运行 ./script/build_and_run.sh，之后直接双击余料仓库.app。'
  read
fi
