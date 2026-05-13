#!/usr/bin/env python3
"""
火山方舟每日用量报告脚本
使用官方 volcengine SDK 处理签名，无需手动实现
所有密钥通过环境变量传入，可安全上传 GitHub
"""

import datetime
import json
import time
import base64
import hmac
import hashlib
import urllib.parse
import os
import sys
import requests

# ============================================================
# 依赖：pip install volcengine requests
# ============================================================

try:
    from volcengine.base.Service import Service
    from volcengine.ServiceInfo import ServiceInfo
    from volcengine.ApiInfo import ApiInfo
    from volcengine.Credentials import Credentials
except ImportError:
    print("❌ 请先安装: pip install volcengine")
    sys.exit(1)

# ============================================================
# 从环境变量读取配置
# ============================================================

def require_env(key):
    val = os.getenv(key)
    if not val:
        print(f"❌ 缺少环境变量: {key}")
        sys.exit(1)
    return val

VOLC_AK             = require_env("VOLC_AK")
VOLC_SK             = require_env("VOLC_SK")
DINGTALK_WEBHOOK    = require_env("DINGTALK_WEBHOOK")
DINGTALK_SECRET     = require_env("DINGTALK_SECRET")
DINGTALK_APP_KEY    = require_env("DINGTALK_APP_KEY")
DINGTALK_APP_SECRET = require_env("DINGTALK_APP_SECRET")

BITABLE_DOC_KEY  = "vy20BglGWOM2YR29C0Pzb2AZJA7depqY"
BITABLE_SHEET_ID = "dUQiuQL"

# 接入点配置
ENDPOINTS = {
    "ep-20260507172115-j6nd7": {"name": "Doubao-Seedream-5.0-lite", "app": "queqi_ai",  "type": "image"},
    "ep-20260430164018-qbt8h": {"name": "Seedance-2.0-fast",        "app": "queqi_ai",  "type": "video"},
    "ep-20260430163938-x9fzm": {"name": "Seedance-2.0",             "app": "queqi_ai",  "type": "video"},
    "ep-20260409141804-kjq8g": {"name": "Doubao-Seed-2.0-mini",     "app": "ScriptGen", "type": "text"},
    "ep-20260409141740-tqj9z": {"name": "Doubao-Seed-2.0-lite",     "app": "ScriptGen", "type": "text"},
    "ep-20260409141700-xvdj5": {"name": "Doubao-Seed-2.0-pro",      "app": "ScriptGen", "type": "text"},
    "ep-20260409140728-wb7qx": {"name": "DeepSeek-V3.2",            "app": "ScriptGen", "type": "text"},
    "ep-20260309174720-z6h28": {"name": "Doubao-Seedream-5.0-lite", "app": "dify",      "type": "image"},
}

# ============================================================
# 构建 Volcengine Service（官方 SDK，自动处理签名）
# ============================================================

def make_ark_service():
    service_info = ServiceInfo(
        "open.volcengineapi.com",
        {"Content-Type": "application/json"},
        Credentials(VOLC_AK, VOLC_SK, "ark", "cn-beijing"),
        10, 10, "https"
    )
    api_info = {
        "GetInferenceUsage": ApiInfo(
            "POST", "/",
            {"Action": "GetInferenceUsage", "Version": "2024-01-01"},
            {}, {}
        )
    }
    return Service(service_info, api_info)

def make_billing_service():
    service_info = ServiceInfo(
        "open.volcengineapi.com",
        {"Content-Type": "application/json"},
        Credentials(VOLC_AK, VOLC_SK, "billing", "cn-north-1"),
        10, 10, "https"
    )
    api_info = {
        "ListBillOverviewByProd": ApiInfo(
            "POST", "/",
            {"Action": "ListBillOverviewByProd", "Version": "2022-01-01"},
            {}, {}
        )
    }
    return Service(service_info, api_info)

# ============================================================
# 数据抓取
# ============================================================

def fetch_ep_usage(ark_svc, ep_id, start_date, end_date):
    body = {
        "QueryInterval": "Day",
        "StartTime":     start_date,
        "EndTime":       end_date,
        "Filters": [{"Key": "ModelEndpoint", "Values": [ep_id]}],
    }
    resp = ark_svc.json("GetInferenceUsage", {}, json.dumps(body))
    if isinstance(resp, str):
        resp = json.loads(resp)
    return resp.get("Result", {})

def fetch_billing(bill_svc, bill_period):
    try:
        body = {"BillPeriod": bill_period, "Limit": 100, "Offset": 0}
        resp = bill_svc.json("ListBillOverviewByProd", {}, json.dumps(body))
        if isinstance(resp, str):
            resp = json.loads(resp)
        items = resp.get("Result", {}).get("List", [])
        return round(sum(float(i.get("PayableAmount", 0)) for i in items), 2)
    except Exception as e:
        print(f"  账单查询失败: {e}")
        return None

def collect_all_data(target_date):
    ark_svc = make_ark_service()
    end = (datetime.date.fromisoformat(target_date) + datetime.timedelta(days=1)).isoformat()
    rows = []
    for ep_id, meta in ENDPOINTS.items():
        try:
            usage  = fetch_ep_usage(ark_svc, ep_id, target_date, end)
            fields = [f["Name"] for f in usage.get("Fields", [])]
            t      = {"InputTokens": 0, "OutputTokens": 0, "TotalTokens": 0, "ReqCnt": 0, "ImageCount": 0}
            for row in usage.get("Data", []):
                d = dict(zip(fields, row))
                for k in t:
                    t[k] += int(d.get(k, 0) or 0)
            rows.append({
                **meta, "ep_id": ep_id,
                "input_tokens":  t["InputTokens"],
                "output_tokens": t["OutputTokens"],
                "total_tokens":  t["TotalTokens"],
                "req_cnt":       t["ReqCnt"],
                "image_count":   t["ImageCount"],
            })
            print(f"  ✓ {meta['name']}({meta['app']}): {t['TotalTokens']:,} tokens, {t['ReqCnt']} 次, {t['ImageCount']} 张")
        except Exception as e:
            print(f"  ✗ {ep_id} 失败: {e}")
            rows.append({**meta, "ep_id": ep_id,
                         "input_tokens": 0, "output_tokens": 0,
                         "total_tokens": 0, "req_cnt": 0, "image_count": 0})
    return rows

# ============================================================
# 钉钉群推送
# ============================================================

def dingtalk_sign():
    ts  = str(round(time.time() * 1000))
    raw = hmac.new(
        DINGTALK_SECRET.encode(),
        f"{ts}\n{DINGTALK_SECRET}".encode(),
        hashlib.sha256
    ).digest()
    sig = urllib.parse.quote_plus(base64.b64encode(raw))
    return ts, sig

def send_dingtalk(text):
    ts, sig = dingtalk_sign()
    resp = requests.post(
        f"{DINGTALK_WEBHOOK}&timestamp={ts}&sign={sig}",
        json={"msgtype": "text", "text": {"content": text}},
        timeout=10,
    )
    r = resp.json()
    print("  ✅ 发送成功" if r.get("errcode") == 0 else f"  ❌ 失败: {r}")

def format_report(date_str, rows, billing):
    wd  = ["周一","周二","周三","周四","周五","周六","周日"][datetime.date.fromisoformat(date_str).weekday()]
    day = datetime.date.fromisoformat(date_str).strftime("%Y/%m/%d")

    active = [r for r in rows if r["total_tokens"] > 0 or r["req_cnt"] > 0 or r["image_count"] > 0]

    lines = [
        f"🔥 火山方舟用量日报  {day}（{wd}）",
        "─" * 34,
        "📊 今日汇总",
        f"   💎 Token总量：{sum(r['total_tokens'] for r in rows):,}",
        f"   📞 调用次数：{sum(r['req_cnt'] for r in rows):,}",
        f"   🖼 生图/帧数：{sum(r['image_count'] for r in rows):,}",
        f"   💸 本月花费：{'¥' + f\"{billing:,.2f}\" if billing is not None else '查询失败'}",
        "",
        "📌 接入点明细",
        f"{'模型(应用)':<30} {'输入Token':>12} {'输出Token':>12} {'总Token':>12} {'调用':>6} {'生图':>6}",
        "─" * 80,
    ]
    for r in active:
        label = f"{r['name']}({r['app']})"
        lines.append(
            f"{label:<30} {r['input_tokens']:>12,} {r['output_tokens']:>12,} "
            f"{r['total_tokens']:>12,} {r['req_cnt']:>6,} {r['image_count']:>6,}"
        )
    if not active:
        lines.append("  （今日无调用）")
    return "\n".join(lines)

# ============================================================
# 钉钉多维表格写入
# ============================================================

def write_to_bitable(date_str, rows, billing):
    try:
        resp  = requests.get(
            "https://oapi.dingtalk.com/gettoken",
            params={"appkey": DINGTALK_APP_KEY, "appsecret": DINGTALK_APP_SECRET},
            timeout=10,
        )
        token = resp.json().get("access_token")
        if not token:
            raise Exception(f"获取 token 失败: {resp.json()}")

        records = [
            {"fields": {
                "日期":     date_str,
                "接入点":   r["ep_id"],
                "模型名称": r["name"],
                "应用":     r["app"],
                "输入Token": r["input_tokens"],
                "输出Token": r["output_tokens"],
                "总Token":  r["total_tokens"],
                "调用次数": r["req_cnt"],
                "生图张数": r["image_count"],
                "本月花费": billing or 0,
            }}
            for r in rows
            if r["total_tokens"] > 0 or r["req_cnt"] > 0 or r["image_count"] > 0
        ]

        if not records:
            print("  今日无数据，跳过写入")
            return

        url = (
            f"https://api.dingtalk.com/v1.0/doc/bitable/"
            f"{BITABLE_DOC_KEY}/sheets/{BITABLE_SHEET_ID}/records/batchCreate"
        )
        r = requests.post(
            url,
            headers={"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"},
            json={"records": records},
            timeout=15,
        )
        print(f"  ✅ 写入成功，{len(records)} 行" if r.status_code == 200 else f"  ❌ 写入失败: {r.json()}")
    except Exception as e:
        print(f"  ⚠️  多维表格跳过（钉钉审批通过后自动生效）: {e}")

# ============================================================
# 主流程
# ============================================================

def main():
    yesterday   = datetime.date.today() - datetime.timedelta(days=1)
    target      = yesterday.isoformat()
    bill_period = yesterday.strftime("%Y-%m")

    print(f"\n{'='*50}")
    print(f"火山方舟日报  查询日期: {target}")
    print(f"{'='*50}\n")

    print("📡 查询接入点用量...")
    rows = collect_all_data(target)

    print("\n💰 查询账单...")
    bill_svc = make_billing_service()
    billing  = fetch_billing(bill_svc, bill_period)
    print(f"  本月花费: ¥{billing}" if billing is not None else "  账单查询失败")

    report = format_report(target, rows, billing)
    print(f"\n{report}\n")

    print("📨 发送钉钉群...")
    send_dingtalk(report)

    print("\n📋 写入多维表格...")
    write_to_bitable(target, rows, billing)

    print("\n✅ 全部完成！")

if __name__ == "__main__":
    main()
