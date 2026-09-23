#!/usr/bin/env bash
# cadence JDK 工具兜底入口（镜像内以软链接安装为 /usr/local/bin/<工具>；见 Dockerfile）
#   为什么需要：PATH 中若 current/bin 解析失败（状态卷损坏、卷未挂载），java/javac 会继续向后匹配到
#   /usr/bin/java（apt 安装的系统 Temurin 21），于是 `java` 静默给出与 jdk 选择不符的版本、而 Maven
#   又因 JAVA_HOME 无效失败——同一次损坏出现两种互相矛盾的表现。
#   本入口在 PATH 中位于 current/bin 之后、/usr/bin 之前：正常状态下 current/bin/<工具> 先命中，本入口
#   不会被调用；损坏状态下在此明确失败（fail-closed），绝不回退到系统 Java。
set -euo pipefail

name="$(basename "$0")"
if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/$name" ]; then
  exec "$JAVA_HOME/bin/$name" "$@"
fi
printf '%s: JDK 状态不可用：JAVA_HOME=%s 下没有可执行的 bin/%s——已阻止回退到系统 Java。用 jdk list 查看，jdk use 21 修复\n' \
  "$name" "${JAVA_HOME:-未设置}" "$name" >&2
exit 127
