"""火山方舟 Plan 用量模组：Coding Plan / Agent Plan 额度查询。

查询方式（实测可用，参考 margrop/coding-plan-dashboard）：
  POST https://open.volcengineapi.com/?Action=GetCodingPlanUsage&Version=2024-01-01
  POST https://open.volcengineapi.com/?Action=GetAgentPlanAFPUsage&Version=2024-01-01
  鉴权：火山引擎 V4 签名（HMAC-SHA256，service=ark, region=cn-beijing），body 为 {}

AK/SK 需要在火山引擎控制台创建具备以下权限的子账号凭据：
  ark:GetCodingPlanUsage（Coding Plan）/ ark:GetAgentPlanAFPUsage（Agent Plan）
"""

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

HOST = "open.volcengineapi.com"
SERVICE = "ark"

LEVEL_NAMES = {"session": "5小时窗口", "weekly": "本周", "monthly": "本月"}
AFP_NAMES = (("AFPFiveHour", "5小时窗口"), ("AFPWeekly", "本周"),
             ("AFPMonthly", "本月"))


def _norm_query(params):
    q = ""
    for key in sorted(params):
        q += (quote(key, safe="-_.~") + "=" +
              quote(str(params[key]), safe="-_.~") + "&")
    return q[:-1]


def _hmac(key, content):
    if isinstance(key, str):
        key = key.encode("utf-8")
    return hmac.new(key, content.encode("utf-8"), hashlib.sha256).digest()


def _sign(ak, sk, action, region, body=b"{}"):
    now = datetime.now(timezone.utc)
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short = x_date[:8]
    body_hash = hashlib.sha256(body).hexdigest()
    headers = {"Host": HOST, "X-Date": x_date,
               "X-Content-Sha256": body_hash,
               "Content-Type": "application/json"}
    signed = {k.lower(): v for k, v in headers.items()
              if k in ("Content-Type", "Host") or k.startswith("X-")}
    signed_str = "".join("%s:%s\n" % (k, signed[k]) for k in sorted(signed))
    sh = ";".join(sorted(signed))
    query = {"Action": action, "Version": "2024-01-01"}
    canonical = "\n".join(["POST", "/", _norm_query(query), signed_str, sh,
                           body_hash])
    scope = "%s/%s/%s/request" % (short, region, SERVICE)
    string_to_sign = "\n".join([
        "HMAC-SHA256", x_date, scope,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(sk, short), region), SERVICE), "request")
    signature = hmac.new(key, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        "HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
        % (ak, scope, sh, signature))
    return "https://%s/?%s" % (HOST, _norm_query(query)), headers, body


def _call(ak, sk, action, region):
    url, headers, body = _sign(ak, sk, action, region)
    req = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except HTTPError as e:
        raise RuntimeError("HTTP %s" % e.code)
    except URLError as e:
        raise RuntimeError("网络错误：%s" % e.reason)
    if isinstance(data, dict) and data.get("ResponseMetadata", {}).get("Error"):
        err = data["ResponseMetadata"]["Error"]
        raise RuntimeError("%s %s" % (err.get("Code", ""), err.get("Message", ""))[:80])
    return data


def _fmt_reset(ts_ms):
    try:
        ts = int(ts_ms)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    ts = ts / 1000.0 if ts > 1e11 else float(ts)
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _pct(used, quota):
    return round(used / quota * 100, 1) if quota else 0.0


def get_payload(config: dict) -> dict:
    """返回火山方舟 Plan 用量页面数据。

    config 字段（manifest.config_schema 声明）：
      plan_type: "coding" | "agent"
      access_key / secret_key: 火山引擎子账号 AK/SK
      region: 默认 cn-beijing
    """
    plan = (config.get("plan_type") or "coding").strip().lower()
    region = (config.get("region") or "cn-beijing").strip() or "cn-beijing"
    ak = (config.get("access_key") or "").strip()
    sk = (config.get("secret_key") or "").strip()
    if not ak or not sk:
        return {"title": "火山方舟 Plan", "error": "未配置 AK/SK"}
    if plan not in ("coding", "agent"):
        plan = "coding"

    action = "GetCodingPlanUsage" if plan == "coding" else "GetAgentPlanAFPUsage"
    try:
        data = _call(ak, sk, action, region)
    except Exception as e:
        return {"title": "火山方舟 %s" % ("Coding Plan" if plan == "coding"
                                          else "Agent Plan"),
                "error": str(e)[:80]}
    result = data.get("Result") or {}

    lines, bars, resets = [], [], []
    if plan == "coding":
        for q in result.get("QuotaUsage") or []:
            level = str(q.get("Level") or "").lower()
            name = LEVEL_NAMES.get(level, level or "额度")
            pct = round(float(q.get("Percent") or 0), 1)
            lines.append([name, "%.1f%%" % pct])
            bars.append([name, pct])
            r = _fmt_reset((q.get("ResetTimestamp") or 0) * 1000)
            if r:
                resets.append("%s %s" % (name.split("窗口")[0], r))
    else:
        for key, name in AFP_NAMES:
            item = result.get(key)
            if not isinstance(item, dict):
                continue
            quota = float(item.get("Quota") or 0)
            used = float(item.get("Used") or 0)
            pct = _pct(used, quota)
            lines.append([name, "%.1f / %.0f（%.1f%%）" % (used, quota, pct)])
            bars.append([name, pct])
            r = _fmt_reset(item.get("ResetTime"))
            if r:
                resets.append("%s %s" % (name.split("窗口")[0], r))

    plan_name = "Coding Plan" if plan == "coding" else "Agent Plan"
    return {
        "title": "火山方舟 %s" % plan_name,
        "subtitle": "更新 %s" % time.strftime("%H:%M"),
        "lines": lines,
        "bars": bars[:4],
        "text": ("最近重置 " + " · ".join(resets[:2])) if resets else "",
    }
