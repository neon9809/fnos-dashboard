"""GLM Coding Plan 用量模组（智谱 bigmodel.cn / 国际站 z.ai）。

查询方式（实测可用，参考 neon9809/api-quota-dashboard）：
  GET {base}/api/monitor/usage/quota/limit        主接口：额度窗口
  GET {base}/api/biz/subscription/list            增强：套餐名 / 续费日期（尽力而为）
  base：国内 https://open.bigmodel.cn，国际 https://api.z.ai
  鉴权：Authorization: Bearer <API Key>（bigmodel.cn 控制台的 key，与推理同一把）

quota/limit → data.limits[]：
  type TOKENS_LIMIT / CREDIT_LIMIT，unit=3 → 5 小时窗口，unit=6 → 周窗口
    字段 percentage（已用 %）、nextResetTime（epoch ms）、
    usage（总量）/ currentValue（已用）/ remaining（剩余，部分套餐只给百分比）
  type TIME_LIMIT → MCP / 联网工具月额度（usageDetails[] 分模型明细）
  data.level → 套餐档位（lite / pro / max）
"""

import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASES = {"cn": "https://open.bigmodel.cn", "intl": "https://api.z.ai"}
QUOTA_PATH = "/api/monitor/usage/quota/limit"
SUB_PATH = "/api/biz/subscription/list"


def _get_json(url, api_key):
    req = Request(url, headers={
        "Authorization": "Bearer %s" % api_key,
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh",
        "User-Agent": "fnos-dashboard/1.0",
    })
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except HTTPError as e:
        raise RuntimeError("HTTP %s" % e.code)
    except URLError as e:
        raise RuntimeError("网络错误：%s" % e.reason)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_reset(v):
    try:
        ts = int(v)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    ts = ts / 1000.0 if ts > 1e11 else float(ts)
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def get_payload(config: dict) -> dict:
    """config: api_key（bigmodel.cn 的 key）、region（cn | intl）。"""
    api_key = (config.get("api_key") or "").strip()
    region = (config.get("region") or "cn").strip().lower()
    base = BASES.get(region, BASES["cn"])
    title = "GLM Coding Plan" + ("（国际站）" if region == "intl" else "")
    if not api_key:
        return {"title": title, "error": "未配置 API Key"}

    try:
        body = _get_json(base + QUOTA_PATH, api_key)
    except Exception as e:
        return {"title": title, "error": str(e)[:80]}
    if body.get("success") is False:
        return {"title": title, "error": str(body.get("msg") or "接口返回错误")[:60]}

    data = body.get("data") if isinstance(body.get("data"), (dict, list)) else body
    limits = data.get("limits") if isinstance(data, dict) else data
    if not isinstance(limits, list):
        return {"title": title, "error": "返回缺少 limits（key 可能未开通 Coding Plan）"}

    level = data.get("level") if isinstance(data, dict) else None
    subtitle = "套餐 %s · 更新 %s" % (level or "-", time.strftime("%H:%M")) \
        if level else "更新 %s" % time.strftime("%H:%M")

    lines, bars, resets = [], [], []
    for item in limits:
        if not isinstance(item, dict):
            continue
        itype = str(item.get("type") or "")
        pct = item.get("percentage")
        try:
            pct = round(float(pct), 1) if pct is not None else None
        except (TypeError, ValueError):
            pct = None
        resets_at = _fmt_reset(item.get("nextResetTime")
                               or item.get("next_reset_time"))
        usage_total = _num(item.get("usage"))
        used = _num(item.get("currentValue") or item.get("current_value"))
        remaining = _num(item.get("remaining"))

        if itype in ("TOKENS_LIMIT", "CREDIT_LIMIT"):
            unit = item.get("unit")
            name = "5小时窗口" if unit == 3 else \
                "周窗口" if unit == 6 else "窗口（unit=%s）" % unit
            detail = ("%g / %g" % (used, usage_total)
                      if used is not None and usage_total else None)
            if pct is None and used is not None and usage_total:
                pct = round(used / usage_total * 100, 1)
            if pct is None:
                continue
            lines.append([name, "%.1f%%（%s）" % (pct, detail) if detail
                          else "%.1f%%" % pct])
            bars.append([name, pct])
            if resets_at:
                resets.append(resets_at)
        elif itype == "TIME_LIMIT":
            # MCP / 联网搜索等工具的月度次数额度
            total = usage_total or 0
            if pct is None and used is not None and total:
                pct = round(used / total * 100, 1)
            sub = []
            for d in item.get("usageDetails") or item.get("usage_details") or []:
                if isinstance(d, dict) and d.get("modelCode"):
                    sub.append("%s %g/%g" % (d.get("modelCode"),
                                             d.get("currentValue", 0),
                                             d.get("usage", 0)))
            if pct is None:
                continue
            lines.append(["MCP·联网月额度", "%.1f%%（剩 %g）" % (pct, remaining)
                          if remaining is not None else "%.1f%%" % pct])
            bars.append(["MCP·联网月额度", pct])
            if sub:
                lines.append(["└ 明细", "，".join(sub)[:24]])
            if resets_at:
                resets.append(resets_at)

    if not lines:
        return {"title": title, "error": "未识别到额度窗口（key 可能未开通 Coding Plan）"}

    return {
        "title": title,
        "subtitle": subtitle[:36],
        "lines": lines[:8],
        "bars": bars[:4],
        "text": ("最近重置 " + " · ".join(resets[:2])) if resets else "",
    }
