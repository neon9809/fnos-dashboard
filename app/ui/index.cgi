#!/bin/sh
# fnOS CGI 入口：把 /cgi/ThirdParty/com.fnos.dashboard/index.cgi/ 下的请求
# 反向代理到本机 dashboard 服务（fnOS 校验 NAS 登录态后才执行本脚本）。
# 实现参照 fn-knock 的公开实现（apps/fn-knock/app/ui/index.cgi）。
TARGET_HOST=127.0.0.1
TARGET_PORT=${TRIM_SERVICE_PORT:-8199}

REQ_URI=${REQUEST_URI:-"/"}
URI_NO_QUERY=${REQ_URI%%\?*}
QUERY_STRING=${QUERY_STRING:-}

# 无尾斜杠的入口 302 到带斜杠形式，保证页面内相对路径可用
case "$URI_NO_QUERY" in
    */index.cgi)
        printf "Status: 302 Found\r\n"
        printf "Location: %s/%s\r\n" "$URI_NO_QUERY" "${QUERY_STRING:+?$QUERY_STRING}"
        printf "Content-Type: text/plain; charset=utf-8\r\n"
        printf "Cache-Control: no-store\r\n\r\n"
        exit 0
        ;;
esac

# 取 index.cgi 之后的子路径：/cgi/.../index.cgi/settings -> /settings
case "$URI_NO_QUERY" in
    *index.cgi*) REL_PATH="${URI_NO_QUERY#*index.cgi}" ;;
    *) REL_PATH="$URI_NO_QUERY" ;;
esac
[ -n "$REL_PATH" ] || REL_PATH="/"
case "$REL_PATH" in
    *..*)
        printf "Status: 400 Bad Request\r\n"
        printf "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        printf "Bad Request\n"
        exit 0
        ;;
esac

TARGET_URL="http://${TARGET_HOST}:${TARGET_PORT}${REL_PATH}"
[ -n "$QUERY_STRING" ] && TARGET_URL="${TARGET_URL}?${QUERY_STRING}"

METHOD=${REQUEST_METHOD:-GET}
set -- -s -X "$METHOD"
[ -n "$HTTP_ACCEPT" ]          && set -- "$@" -H "accept: $HTTP_ACCEPT"
[ -n "$HTTP_ACCEPT_LANGUAGE" ] && set -- "$@" -H "accept-language: $HTTP_ACCEPT_LANGUAGE"
[ -n "$HTTP_USER_AGENT" ]      && set -- "$@" -H "user-agent: $HTTP_USER_AGENT"
[ -n "$HTTP_ORIGIN" ]          && set -- "$@" -H "origin: $HTTP_ORIGIN"
[ -n "$HTTP_REFERER" ]         && set -- "$@" -H "referer: $HTTP_REFERER"
case "$METHOD" in
    POST|PUT|PATCH|DELETE)
        set -- "$@" -H "Content-Type: ${CONTENT_TYPE:-application/json}"
        set -- "$@" --data-binary @-
        ;;
esac

HEADER_FILE=$(mktemp) || exit 1
BODY_FILE=$(mktemp) || { rm -f "$HEADER_FILE"; exit 1; }
trap 'rm -f "$HEADER_FILE" "$BODY_FILE"' EXIT

curl "$@" -D "$HEADER_FILE" -o "$BODY_FILE" "$TARGET_URL" || {
    printf "Status: 502 Bad Gateway\r\n"
    printf "Content-Type: text/plain; charset=utf-8\r\n\r\n"
    printf "后端服务未响应，请在应用中心重启该应用。\n"
    exit 0
}

CODE=$(head -1 "$HEADER_FILE" | awk '{print $2}')
[ -n "$CODE" ] && [ "$CODE" != "200" ] && printf "Status: %s\r\n" "$CODE"
grep -i "^Content-Type:" "$HEADER_FILE" | tail -1 | tr -d '\r'
printf "Cache-Control: no-store\r\n"
printf "\r\n"
cat "$BODY_FILE"
