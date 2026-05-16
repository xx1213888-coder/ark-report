#!/usr/bin/env python3
"""
火山方舟用量日报 — 完整版（签名修复后）
========================================
每天自动：拉取用量 → 推送钉钉群 → 写入多维表格

用法：python3 volc_ark_report.py [YYYY-MM-DD]
默认查昨天。可从命令行指定日期。

已修复的签名关键点：
  1. SK 用 base64 字符串的 UTF-8 字节直接做 HMAC，不解码
  2. POST 请求，Action/Version 在 Query，业务参数在 JSON Body
  3. SignedHeaders = content-type;host;x-content-sha256;x-date
"""

import os
import sys
import json
import time
import base64
import hmac
import hashlib
import urllib.parse
from datetime import datetime, timedelta, timezone

# 依赖检查
try:
    import requests
except ImportError:
    print("请先安装: pip3 install requests --break-system-packages")
    sys.exit(1)


# ═══════════════════════════════════════════════
# 配置（GitHub Secrets / 环境变量）
# ═══════════════════════════════════════════════

VOLC_AK = os.getenv("VOLC_AK", "")
VOLC_SK = os.getenv("VOLC_SK", "")

DINGTALK_WEBHOOK = os.getenv("DINGTALK_WEBHOOK", "")
DINGTALK_SECRET = os.getenv("DINGTALK_SECRET", "")
DINGTALK_APP_KEY = os.getenv("DINGTALK_APP_KEY", "")
DINGTALK_APP_SECRET = os.getenv("DINGTALK_APP_SECRET", "")

BITABLE_WEBHOOK = os.getenv("BITABLE_WEBHOOK", "")

# 接入点列表
ENDPOINTS = {
    "ep-20260507172115-j6nd7": {"name": "Doubao-Seedream-5.0-lite", "app": "queqi_ai", "type": "image", "label": "queqi_ai_Doubao-Seedream-5.0-lite"},
    "ep-20260430164018-qbt8h": {"name": "Seedance-2.0-fast",        "app": "queqi_ai", "type": "video", "label": "queqi_ai_Seedance2.0_fast"},
    "ep-20260430163938-x9fzm": {"name": "Seedance-2.0",             "app": "queqi_ai", "type": "video", "label": "queqi_ai_Seedance2.0"},
    "ep-20260409141804-kjq8g": {"name": "Doubao-Seed-2.0-mini",     "app": "ScriptGen", "type": "text",  "label": "ScriptGen-App-Doubao-Seed-2.0-mini"},
    "ep-20260409141740-tqj9z": {"name": "Doubao-Seed-2.0-lite",     "app": "ScriptGen", "type": "text",  "label": "ScriptGen-App-Doubao-Seed-2.0-lite"},
    "ep-20260409141700-xvdj5": {"name": "Doubao-Seed-2.0-pro",      "app": "ScriptGen", "type": "text",  "label": "ScriptGen-App-Doubao-Seed-2.0-pro"},
    "ep-20260409140728-wb7qx": {"name": "DeepSeek-V3.2",            "app": "ScriptGen", "type": "text",  "label": "ScriptGen-App-DeepSeek-V3.2"},
    "ep-20260309174720-z6h28": {"name": "Doubao-Seedream-5.0-lite", "app": "dify",      "type": "image", "label": "dify"},
}


# ═══════════════════════════════════════════════
# 火山引擎 HMAC-SHA256 签名
# ═══════════════════════════════════════════════

def volcengine_sign(ak, sk, action, version, body, region="cn-beijing", service="ark"):
    """火山引擎 IAM HMAC-SHA256 签名
    关键：SK 是 base64 字符串，直接用 .encode('utf-8')，不解码！
    """
    t = datetime.now(timezone.utc)
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]

    # Body hash
    payload_hash = hashlib.sha256(body.encode()).hexdigest()

    # Signed headers（字母序、全小写）
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_headers = (
        f"content-type:application/json\n"
        f"host:open.volcengineapi.com\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{amz_date}\n"
    )

    # Canonical request
    canonical_request = (
        f"POST\n"
        f"/\n"
        f"Action={action}&Version={version}\n"
        f"{canonical_headers}\n"
        f"{signed_headers}\n"
        f"{payload_hash}"
    )
    hash_cr = hashlib.sha256(canonical_request.encode()).hexdigest()

    # String to sign
    string_to_sign = (
        f"HMAC-SHA256\n"
        f"{amz_date}\n"
        f"{date_stamp}/{region}/{service}/request\n"
        f"{hash_cr}"
    )

    # 签名密钥：SK 作为 UTF-8 bytes（不解码 base64！）
    sk_bytes = sk.encode('utf-8')
    k_date    = hmac.new(sk_bytes, date_stamp.encode(), hashlib.sha256).digest()
    k_region  = hmac.new(k_date,    region.encode(),      hashlib.sha256).digest()
    k_service = hmac.new(k_region,  service.encode(),     hashlib.sha256).digest()
    k_sign    = hmac.new(k_service, b'request',           hashlib.sha256).digest()
    signature = hmac.new(k_sign, string_to_sign.encode(), hashlib.sha256).hexdigest()

    authorization = (
        f"HMAC-SHA256 "
        f"Credential={ak}/{date_stamp}/{region}/{service}/request, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )

    return {
        "X-Date": amz_date,
        "X-Content-Sha256": payload_hash,
        "Authorization": authorization,
        "Content-Type": "application/json",
    }


def volc_request(action, version, payload, region="cn-beijing", service="ark"):
    """发送火山引擎 API 请求"""
    body = json.dumps(payload)
    headers = volcengine_sign(VOLC_AK, VOLC_SK, action, version, body, region, service)
    url = f"https://open.volcengineapi.com/?Action={action}&Version={version}"
    resp = requests.post(url, headers=headers, data=body, timeout=30)
    return resp.json()


# ═══════════════════════════════════════════════
# 数据抓取
# ═══════════════════════════════════════════════

def fetch_ep_usage(ep_id, target_date):
    """抓取某接入点某日的用量（汇总当天所有行）"""
    end_date_dt = datetime.strptime(target_date, "%Y-%m-%d") + timedelta(days=1)
    end_date = end_date_dt.strftime("%Y-%m-%d")

    payload = {
        "QueryInterval": "Day",
        "StartTime": target_date,
        "EndTime": end_date,
        "Limit": 1000,
        "Filters": [{"Key": "ModelEndpoint", "Values": [ep_id]}],
    }
    resp = volc_request("GetInferenceUsage", "2024-01-01", payload)
    result = resp.get("Result", {})

    if not result or not result.get("Data"):
        return None

    fields = [f["Name"] for f in result.get("Fields", [])]
    total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
             "req_cnt": 0, "image_count": 0, "video_count": 0, "frame_count": 0}
    match_count = 0
    for row in result["Data"]:
        data = dict(zip(fields, row))
        if data.get("Day") == target_date:
            match_count += 1
            total["input_tokens"]  += int(data.get("InputTokens", 0) or 0)
            total["output_tokens"] += int(data.get("OutputTokens", 0) or 0)
            total["total_tokens"]  += int(data.get("TotalTokens", 0) or 0)
            total["req_cnt"]       += int(data.get("ReqCnt", 0) or 0)
            total["image_count"]   += int(data.get("ImageCount", 0) or 0)
            total["video_count"]   += int(data.get("VideoCount", 0) or 0)
            total["frame_count"]   += int(data.get("FrameCount", 0) or 0)

    if match_count == 0:
        return None
    if match_count > 1:
        print(f"（{match_count} 行汇总）", end="")
    return total


def fetch_total_usage(target_date):
    """不设接入点过滤，拿当日总量（绕过 API 单点过滤不准的问题）"""
    end_date_dt = datetime.strptime(target_date, "%Y-%m-%d") + timedelta(days=1)
    end_date = end_date_dt.strftime("%Y-%m-%d")
    payload = {
        "QueryInterval": "Day",
        "StartTime": target_date,
        "EndTime": end_date,
        "Limit": 1000,
    }
    resp = volc_request("GetInferenceUsage", "2024-01-01", payload)
    result = resp.get("Result", {})
    if not result or not result.get("Data"):
        return None
    fields = [f["Name"] for f in result.get("Fields", [])]
    for row in result["Data"]:
        data = dict(zip(fields, row))
        if data.get("Day") == target_date:
            return {
                "input_tokens":  int(data.get("InputTokens", 0) or 0),
                "output_tokens": int(data.get("OutputTokens", 0) or 0),
                "total_tokens":  int(data.get("TotalTokens", 0) or 0),
                "req_cnt":       int(data.get("ReqCnt", 0) or 0),
                "image_count":   int(data.get("ImageCount", 0) or 0),
                "video_count":   0,
                "frame_count":   0,
            }
    return None


def fetch_billing(bill_period):
    """抓取月度账单，按产品拆分，返回 (总花费, ARK模型花费)"""
    payload = {"BillPeriod": bill_period, "Limit": 100, "Offset": 0}
    resp = volc_request("ListBillOverviewByProd", "2022-01-01", payload,
                        region="cn-north-1", service="billing")
    items = resp.get("Result", {}).get("List", [])
    total = sum(float(item.get("PayableAmount", 0)) for item in items)

    # 打印每个产品，方便确认 ARK 的产品名
    ark_total = 0.0
    ark_keywords = ["ark", "doubao", "seedream", "seedance", "deepseek", "方舟", "推理", "模型"]
    print("   产品账单明细：")
    for item in items:
        product = item.get("Product") or item.get("ProductZh") or item.get("ProductName", "未知产品")
        amount = float(item.get("PayableAmount", 0))
        print(f"     {product}: ¥{amount:,.2f}")
        # 匹配 ARK 相关的产品
        if any(kw.lower() in str(product).lower() for kw in ark_keywords):
            ark_total += amount

    return round(total, 2), round(ark_total, 2)


# ═══════════════════════════════════════════════
# 16API 用量抓取
# ═══════════════════════════════════════════════

API16_USER = os.getenv("API16_USER", "")
API16_PASS = os.getenv("API16_PASS", "")
API16_USER_ID = "325536"

# 16API 关心的 token → 显示名（未列出的直接用原始 key 名）
API16_TOKENS = {
    "queqi_ai_chatimage": "queqi_ai_chatimage",
    "queqi_ai_nano":      "queqi_ai_nano",
    "gpt-image-2":        "gpt-image-2",
    "noteai":             "noteai",
    "elza的初始令牌":       "elza的初始令牌",
}


def fetch_16api_usage(target_date):
    """抓取 16API 某日的用量（按 token/model 聚合）
    返回 (rows, monthly_quota_sum) — rows 是当天明细，monthly_quota_sum 是当月累计额度
    """
    tz = timezone(timedelta(hours=8))  # UTC+8
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    start_ts = int(dt.replace(hour=0, minute=0, second=0, tzinfo=tz).timestamp())
    end_ts   = int(dt.replace(hour=23, minute=59, second=59, tzinfo=tz).timestamp())

    # 当月起止时间戳（用于累加月度账单）
    month_start = dt.replace(day=1)
    month_start_ts = int(month_start.replace(hour=0, minute=0, second=0, tzinfo=tz).timestamp())
    # 下月第一天 00:00:00
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1, day=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1, day=1)
    month_end_ts = int(next_month.replace(hour=0, minute=0, second=0, tzinfo=tz).timestamp()) - 1

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "New-Api-User": API16_USER_ID,
    })

    # 登录
    resp = session.post(
        "https://www.16api.com/api/user/login",
        json={"username": API16_USER, "password": API16_PASS},
        timeout=15,
    )
    if not resp.json().get("success"):
        print("   ❌ 16API 登录失败")
        return [], 0

    # 全量拉取所有页，客户端按日期过滤（不用服务端时间过滤，避免漏数据）
    all_items = []
    seen_ids = set()
    monthly_quota_sum = 0  # 当月累计消费额度
    full_total = 0
    page = 1
    while True:
        resp = session.get(
            "https://www.16api.com/api/log/self",
            params={"page": page, "page_size": 100},
            timeout=15,
        )
        data = resp.json()
        items = data.get("data", {}).get("items", [])
        if not items:
            break
        full_total = data.get("data", {}).get("total", 0)

        in_range = 0
        page_first_id = None
        for item in items:
            ts = item.get("created_at", 0)
            if start_ts <= ts <= end_ts:
                in_range += 1
                item_id = item.get("id")
                if not page_first_id:
                    page_first_id = item_id
                if item_id and item_id in seen_ids:
                    continue
                if item_id:
                    seen_ids.add(item_id)
                all_items.append(item)
                # 目标日条目一定在当月，直接累加（已去重）
                if month_start_ts <= ts <= month_end_ts:
                    monthly_quota_sum += int(item.get("quota", 0))

            # 不在目标日但在当月：也要计入月度累计（独立去重）
            elif month_start_ts <= ts <= month_end_ts:
                item_id = item.get("id")
                if item_id and item_id in seen_ids:
                    continue
                if item_id:
                    seen_ids.add(item_id)
                monthly_quota_sum += int(item.get("quota", 0))

        first_ts = items[0].get("created_at", 0)
        last_ts = items[-1].get("created_at", 0)
        first_str = datetime.fromtimestamp(first_ts, tz=tz).strftime("%m-%d %H:%M:%S")
        last_str = datetime.fromtimestamp(last_ts, tz=tz).strftime("%m-%d %H:%M:%S")
        print(f"   Page {page}: {in_range} 在范围内 / {len(items)} 条（{first_str} ~ {last_str}），"
              f"total={full_total}")

        # 拉完所有页
        if page * 100 >= full_total:
            break
        page += 1
        if page > 50:
            break

    print(f"   日志拉取: {len(all_items)} 条（日期范围内，全量 {full_total} 条，{page} 页）")
    print(f"   当月累计 quota: {monthly_quota_sum:,} ≈ ¥{monthly_quota_sum/500000*7.2:,.2f}")

    # Debug: 打印所有条目明细
    if all_items:
        print(f"\n   🔑 API 返回字段: {list(all_items[0].keys())}")
    print(f"\n   📋 全部 {len(all_items)} 条目明细：")
    from collections import Counter
    token_cnt = Counter()
    for item in sorted(all_items, key=lambda x: x.get("created_at", 0)):
        ts_str = datetime.fromtimestamp(item["created_at"], tz=tz).strftime("%H:%M:%S")
        other_raw = item.get("other", "{}")
        try:
            other = json.loads(other_raw) or {}  # ← 修复：json.loads("null") 返回 None，加 or {} 兜底
        except:
            other = {}
        has_retry = "used_channels" in other or "retry_count" in other
        img_out = other.get("image_output", "?")
        retry = other.get("retry_count", "?")
        itok = item.get("input_tokens") or item.get("prompt_tokens") or 0
        otok = item.get("output_tokens") or item.get("completion_tokens") or 0
        print(f"   {ts_str} | {item['token_name']:<25} | {item['model_name']:<35} | "
              f"quota={item['quota']:>10,} | in={itok:>6,} out={otok:>6,} | "
              f"img_out={img_out:>5} | retry={retry:>3} | "
              f"{'🔁重试' if has_retry else '✅独立'}")
        token_cnt[item["token_name"]] += 1
    print(f"\n   按 token 分布: {dict(token_cnt)}")

    # 按 token 聚合原始条目
    from collections import defaultdict as dd
    stats = dd(lambda: {"calls": 0, "itok": 0, "otok": 0, "quota": 0, "model": "", "label": "", "entries": []})
    for item in all_items:
        token = item.get("token_name", "")
        if not token:
            continue
        model = item.get("model_name", "")
        label = API16_TOKENS.get(token, token)
        stats[token]["model"] = model
        stats[token]["label"] = label
        stats[token]["entries"].append(item)
        stats[token]["quota"] += int(item.get("quota", 0))
        stats[token]["itok"]  += int(item.get("input_tokens") or item.get("prompt_tokens") or 0)
        stats[token]["otok"]  += int(item.get("output_tokens") or item.get("completion_tokens") or 0)

    # ── 去重 ──
    # 策略：对于有 retry 标记的条目，同一 image_output 值 = 同一请求（不限时间窗口）
    #       对于无 retry 标记且 img_out=0 的条目（如 gemini），每条都是独立请求
    print(f"\n   🔍 去重过程：")
    result = []
    for token, s in stats.items():
        entries = sorted(s["entries"], key=lambda x: x.get("created_at", 0))

        # 把条目分成两类：有 retry 标记的，和没有的
        retry_entries = []   # 有 used_channels 或 retry_count
        normal_entries = []  # 独立请求

        for e in entries:
            try:
                other = json.loads(e.get("other", "{}")) or {}  # ← 修复：同上
            except:
                other = {}
            has_retry = "used_channels" in other or "retry_count" in other
            if has_retry:
                retry_entries.append((e, other))
            else:
                normal_entries.append((e, other))

        # 对于有 retry 标记的：按 image_output 值分组（img_out>0 才分组，=0 的每条独立）
        img_groups = {}
        for e, other in retry_entries:
            img_out = other.get("image_output", 0)
            if img_out == 0:
                # gemini 等模型 image_output 始终为 0，无法用 0 区分请求
                # 每条重试记录视为独立请求
                key = f"_retry_{e.get('id', id(e))}"
            else:
                key = img_out
            if key not in img_groups:
                img_groups[key] = []
            img_groups[key].append((e, other))

        # 对于无 retry 标记的：如果 img_out 和某个 retry 组匹配 → 是原请求，不单独计数
        unmatched_normal = []
        for e, other in normal_entries:
            img_out = other.get("image_output", 0)
            if img_out > 0 and img_out in img_groups:
                # 这个独立条目是某个重试组的原请求，合并到该组
                img_groups[img_out].append((e, other))
            else:
                unmatched_normal.append((e, other))

        unique_calls = len(img_groups) + len(unmatched_normal)

        print(f"   ── {token} ({len(entries)} 条原始 / {len(retry_entries)}重试 {len(normal_entries)}独立) ──")
        for key, group in sorted(img_groups.items(), key=lambda x: str(x[0])):
            ts_list = [datetime.fromtimestamp(e["created_at"], tz=tz).strftime("%H:%M:%S")
                       for e, _ in group]
            if isinstance(key, str) and key.startswith("_retry_"):
                print(f"      独立重试: {len(group)} 条（{' → '.join(ts_list)}）")
            else:
                print(f"      请求 img_out={key}: {len(group)} 条（{' → '.join(ts_list)}）")
        for e, other in unmatched_normal:
            ts_str = datetime.fromtimestamp(e["created_at"], tz=tz).strftime("%H:%M:%S")
            print(f"      独立请求 img_out={other.get('image_output',0)}: {ts_str}")
        print(f"      → {unique_calls} 次调用\n")

        result.append({
            "label": s["label"],
            "token": token,
            "model": s["model"],
            "calls": unique_calls,
            "itok": s["itok"],
            "otok": s["otok"],
            "quota": s["quota"],
            "images": unique_calls,
        })
    return result, monthly_quota_sum


# ═══════════════════════════════════════════════
# 钉钉推送
# ═══════════════════════════════════════════════

def dingtalk_sign():
    ts = str(int(time.time() * 1000))
    raw = hmac.new(DINGTALK_SECRET.encode(), f"{ts}\n{DINGTALK_SECRET}".encode(),
                   hashlib.sha256).digest()
    sig = urllib.parse.quote_plus(base64.b64encode(raw).decode())
    return ts, sig


def send_dingtalk(text, msgtype="markdown"):
    ts, sig = dingtalk_sign()
    if msgtype == "markdown":
        payload = {"msgtype": "markdown", "markdown": {"title": "火山方舟用量日报", "text": text}}
    else:
        payload = {"msgtype": "text", "text": {"content": text}}
    resp = requests.post(
        f"{DINGTALK_WEBHOOK}&timestamp={ts}&sign={sig}",
        json=payload,
        timeout=10,
    )
    return resp.json()


def write_to_bitable_webhook(date_str, rows, billing, ark_billing=None, prev_billing=None, prev_ark=None, rows_16api=None, api16_billing=None):
    """通过钉钉多维表格 Webhook 自动化写入（绕开 REST API）"""
    success = 0
    fail = 0

    def send_one(payload):
        nonlocal success, fail
        try:
            resp = requests.post(BITABLE_WEBHOOK, json=payload,
                                headers={"Content-Type": "application/json"}, timeout=15)
            if resp.status_code == 200:
                success += 1
            else:
                fail += 1
                print(f"  ⚠ 写入失败: {resp.status_code} {resp.text[:100]}")
        except Exception as e:
            fail += 1
            print(f"  ⚠ 请求异常: {e}")

    for r in rows:
        payload = {
            "keyword": "ark_report",
            "date": date_str,
            "ep_name": r.get("label", r["ep_id"]),
            "model": r["name"],
            "app": r["app"],
            "type": r["type"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "total_tokens": r["total_tokens"],
            "req_id": r["req_cnt"],
            "image_count": r["images"],
            "billing": billing or 0,
            "ark_billing": ark_billing or 0,
            "prev_billing": prev_billing or 0,
            "prev_ark": prev_ark or 0,
        }
        send_one(payload)

    # 16API 记录
    if rows_16api:
        for r in rows_16api:
            payload = {
                "keyword": "ark_report",
                "date": date_str,
                "ep_name": r["label"],      # API key 名称 → 接入点列
                "model": r["model"],        # 模型名 → 模型列
                "app": "16api",
                "type": "image",
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,          # 16API 无 token 统计，留 0
                "req_id": r["calls"],
                "image_count": r["images"],
                "billing": api16_billing or 0,
                "ark_billing": api16_billing or 0,   # 16API 总花费=模型花费
                "prev_billing": 0,
                "prev_ark": 0,
            }
            send_one(payload)

    if fail == 0:
        print(f" ✅ 写入成功: {success} 行")
    else:
        print(f" ⚠ 写入完成: {success} 成功, {fail} 失败")


# ═══════════════════════════════════════════════
# 格式化日报
# ═══════════════════════════════════════════════

def fmt(n):
    return f"{n:,}" if n else "0"


SEP = "—————————"

def build_volc_message(date_str, rows, billing, ark_billing=None, prev_billing=None, prev_ark=None):
    """生成火山方舟日报（独立消息）"""
    wd_map = ["一", "二", "三", "四", "五", "六", "日"]
    weekday = wd_map[datetime.strptime(date_str, "%Y-%m-%d").weekday()]
    day = datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y/%m/%d")

    total_tokens = sum(r["total_tokens"] for r in rows)
    total_calls  = sum(r["req_cnt"] for r in rows)
    total_images = sum(r["images"] for r in rows)

    lines = []
    lines.append(f"AI 调用日报-火山方舟：{day}（周{weekday}）")
    lines.append(SEP)
    lines.append(f"💎 Token总量：{fmt(total_tokens)}")
    lines.append(f"📞 调用次数：{fmt(total_calls)}")
    lines.append(f"🖼 生图数：{fmt(total_images)}")
    if billing:
        lines.append(f"💸 本月总花费：¥{billing:,.2f}")
        if ark_billing is not None:
            lines.append(f"🤖 本月模型调用：¥{ark_billing:,.2f}")
    if prev_billing:
        lines.append(f"📅 上月总花费：¥{prev_billing:,.2f}")
        if prev_ark is not None:
            lines.append(f"🤖 上月模型调用：¥{prev_ark:,.2f}")

    # 过滤有数据的接入点
    active = [r for r in rows if not (
        r["input_tokens"] == 0 and r["output_tokens"] == 0 and
        r["total_tokens"] == 0 and r["req_cnt"] == 0 and r["images"] == 0
    )]

    if active:
        lines.append("")
        lines.append("📌 接入点明细")
        for i, r in enumerate(active):
            if i > 0:
                lines.append(SEP)
            lines.append(f"{r['name']}({r['app']})")
            lines.append(f"输入：{fmt(r['input_tokens'])}")
            lines.append(f"输出：{fmt(r['output_tokens'])}")
            lines.append(f"总token：{fmt(r['total_tokens'])}")
            lines.append(f"调用次数：{fmt(r['req_cnt'])}")
            lines.append(f"生图张数：{fmt(r['images'])}")

    return "\n\n".join(lines)


def build_16api_message(date_str, rows_16api, api16_billing):
    """生成 16API 日报（独立消息），若无数据返回 None"""
    if not rows_16api:
        return None

    wd_map = ["一", "二", "三", "四", "五", "六", "日"]
    weekday = wd_map[datetime.strptime(date_str, "%Y-%m-%d").weekday()]
    day = datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y/%m/%d")

    api16_tokens = sum(r.get("itok", 0) + r.get("otok", 0) for r in rows_16api)
    api16_calls  = sum(r["calls"] for r in rows_16api)
    api16_images = sum(r["images"] for r in rows_16api)

    lines = []
    lines.append(f"AI 调用日报-16API：{day}（周{weekday}）")
    lines.append(f"💎 Token总量：{fmt(api16_tokens)}")
    lines.append(f"📞 调用次数：{fmt(api16_calls)}")
    lines.append(f"🖼 生图数：{fmt(api16_images)}")
    if api16_billing:
        lines.append(f"💸 本月总花费：¥{api16_billing:,.2f}")
        lines.append(f"🤖 本月模型调用：¥{api16_billing:,.2f}")

    # 过滤有数据的接入点
    active = [r for r in rows_16api if not (
        r["calls"] == 0 and r.get("itok", 0) + r.get("otok", 0) == 0 and r["images"] == 0
    )]

    if active:
        lines.append("")
        lines.append("📌 接入点明细")
        for i, r in enumerate(active):
            if i > 0:
                lines.append(SEP)
            tok = r.get("itok", 0) + r.get("otok", 0)
            lines.append(f"{r['label']}({r['model']})")
            lines.append(f"输入：{fmt(r.get('itok', 0))}")
            lines.append(f"输出：{fmt(r.get('otok', 0))}")
            lines.append(f"总token：{fmt(tok)}")
            lines.append(f"调用次数：{fmt(r['calls'])}")
            lines.append(f"生图张数：{fmt(r['images'])}")
            est = r["quota"] / 500000 * 7.2
            lines.append(f"约合：¥{est:,.2f}")

    return "\n\n".join(lines)


def save_daily_csv(date_str, rows, rows_16api, billing, ark_billing, prev_billing, prev_ark, api16_billing):
    """存一份当日 CSV 到桌面"""
    import csv as _csv
    csv_path = f"/Users/xiangxiang/Desktop/日报_{date_str}.csv"
    csv_rows = []

    # 火山行
    for r in rows:
        if r["req_cnt"] == 0 and r["total_tokens"] == 0 and r["images"] == 0:
            continue
        csv_rows.append([
            date_str, r["label"], r["name"], r["req_cnt"],
            r["total_tokens"], r["input_tokens"], r["output_tokens"],
            r["images"],
            billing or 0, ark_billing or 0, prev_billing or 0, prev_ark or 0,
        ])
    # 16API 行
    if rows_16api:
        for r in rows_16api:
            tok = r.get("itok", 0) + r.get("otok", 0)
            csv_rows.append([
                date_str, f"16API_{r['label']}", r["model"], r["calls"],
                tok, r.get("itok", 0), r.get("otok", 0),
                r["images"],
                api16_billing or 0, api16_billing or 0, 0, 0,
            ])

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = _csv.writer(f)
        w.writerow(["日期", "接入点", "模型", "调用次数", "总Token", "输入Token",
                     "输出Token", "生图总张数", "本月已花费", "模型调用花费",
                     "上月总花费", "上月模型花费"])
        for r in csv_rows:
            w.writerow(r)
    print(f"   📄 已存: {csv_path}")


# ═══════════════════════════════════════════════
# 主程序
# ═══════════════════════════════════════════════

REQUIRED_ENV = {
    "VOLC_AK": VOLC_AK,
    "VOLC_SK": VOLC_SK,
    "DINGTALK_WEBHOOK": DINGTALK_WEBHOOK,
    "DINGTALK_SECRET": DINGTALK_SECRET,
    "API16_USER": API16_USER,
    "API16_PASS": API16_PASS,
}

def main():
    # 检查必要环境变量
    missing = [k for k, v in REQUIRED_ENV.items() if not v]
    if missing:
        print(f"❌ 缺少环境变量: {', '.join(missing)}")
        print("   请在 GitHub Secrets 或本地环境中设置这些变量。")
        sys.exit(1)

    # 日期：默认昨天，支持命令行指定
    if len(sys.argv) > 1:
        target_date = sys.argv[1]
    else:
        target_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    bill_period = target_date[:7]

    print(f"\n{'=' * 50}")
    print(f"  火山方舟用量日报")
    print(f"  查询日期: {target_date}")
    print(f"{'=' * 50}\n")

    # ── 1. 拉取各接入点用量 ──
    print("📡 查询各接入点用量...")
    rows = []
    for ep_id, meta in ENDPOINTS.items():
        print(f"   → {meta['name']}({meta['app']}) ", end="", flush=True)
        result = fetch_ep_usage(ep_id, target_date)
        if result:
            vid_count = result["video_count"] + result["frame_count"]
            images = result["image_count"] if meta["type"] == "image" else vid_count
            rows.append({
                **meta, "ep_id": ep_id,
                "input_tokens":  result["input_tokens"],
                "output_tokens": result["output_tokens"],
                "total_tokens":  result["total_tokens"],
                "req_cnt":       result["req_cnt"],
                "images":        images,
            })
            print(f"✓ {result['total_tokens']:,}t {result['req_cnt']}c {images}i")
        else:
            rows.append({
                **meta, "ep_id": ep_id,
                "input_tokens": 0, "output_tokens": 0,
                "total_tokens": 0, "req_cnt": 0, "images": 0,
            })
            print("✗ 无数据")
        time.sleep(0.3)

    # 补缺口：API 单点过滤漏数据，差额补到 queqi_ai Seedream 行
    total_usage = fetch_total_usage(target_date)
    if total_usage:
        sum_calls = sum(r["req_cnt"] for r in rows)
        sum_images = sum(r["images"] for r in rows)
        gap_calls = total_usage["req_cnt"] - sum_calls
        gap_images = total_usage["image_count"] - sum_images
        if gap_calls > 0 or gap_images > 0:
            # 找到 queqi_ai 的 Doubao-Seedream 行，把缺口补进去
            for r in rows:
                if r["label"] == "queqi_ai_Doubao-Seedream-5.0-lite":
                    r["req_cnt"] += gap_calls
                    r["images"] += gap_images
                    print(f"   → ⚠ 补齐缺口 {gap_calls}调用 {gap_images}生图 到 Seedream")
                    break

    # ── 2. 拉取账单 ──
    print("\n💰 查询本月账单...")
    billing, ark_billing = fetch_billing(bill_period)
    if billing:
        print(f"   总花费: ¥{billing:,.2f}  |  模型调用: ¥{ark_billing:,.2f}")
    else:
        print("   查询失败")

    # 上月
    prev_month = (datetime.strptime(target_date, "%Y-%m-%d").replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    print(f"\n📅 查询上月账单（{prev_month}）...")
    prev_billing, prev_ark = fetch_billing(prev_month)
    if prev_billing:
        print(f"   总花费: ¥{prev_billing:,.2f}  |  模型调用: ¥{prev_ark:,.2f}")
    else:
        print("   查询失败")

    # ── 3. 拉取 16API 用量 ──
    print("\n🌐 查询 16API 用量...")
    rows_16api, monthly_quota = fetch_16api_usage(target_date)
    api16_billing = round(monthly_quota / 500000 * 7.2, 2) if monthly_quota else 0  # quota → CNY
    if rows_16api:
        for r in rows_16api:
            est = r["quota"] / 500000 * 7.2
            total_tok = r.get("itok", 0) + r.get("otok", 0)
            print(f"   → {r['label']} 调用:{r['calls']} 输入:{r.get('itok',0):,} 输出:{r.get('otok',0):,} 总Token:{total_tok:,} 配额:{r['quota']:,} ≈¥{est:,.2f}")
        print(f"   当月 16API 累计配额: {monthly_quota:,} ≈ ¥{api16_billing:,.2f}")
    else:
        print("   无数据或查询失败")

    # ── 4. 存 CSV（仅本地） + 生成日报推钉钉 ──
    if os.getenv("GITHUB_ACTIONS") == "true":
        print("\n📄 GitHub Actions 环境，跳过 CSV 保存。")
    else:
        print("\n📄 保存日报 CSV...")
        save_daily_csv(target_date, rows, rows_16api, billing, ark_billing, prev_billing, prev_ark, api16_billing)

    print("\n📨 发送钉钉群消息...")

    # 消息1：火山方舟
    volc_msg = build_volc_message(target_date, rows, billing, ark_billing, prev_billing, prev_ark)
    print(volc_msg)
    print()
    r1 = send_dingtalk(volc_msg)
    if r1.get("errcode") == 0:
        print("  ✅ 火山方舟消息发送成功")
    else:
        print(f"  ❌ 火山方舟发送失败: {r1}")

    # 消息2：16API
    api16_msg = build_16api_message(target_date, rows_16api, api16_billing)
    if api16_msg:
        print()
        print(api16_msg)
        print()
        r2 = send_dingtalk(api16_msg)
        if r2.get("errcode") == 0:
            print("  ✅ 16API 消息发送成功")
        else:
            print(f"  ❌ 16API 发送失败: {r2}")

    # ── 5. 写入多维表格（Webhook 方式） ──
    if BITABLE_WEBHOOK:
        print("\n📋 写入钉钉多维表格...")
        write_to_bitable_webhook(target_date, rows, billing, ark_billing, prev_billing, prev_ark, rows_16api, api16_billing)
    else:
        print("\n📋 未配置 BITABLE_WEBHOOK，跳过表格写入。")

    print(f"\n{'=' * 50}")
    print("  ✅ 全部完成！")
    print(f"{'=' * 50}\n")


if __name__ == "__main__":
    main()
