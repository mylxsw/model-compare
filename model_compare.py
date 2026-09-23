#!/usr/bin/env python3
"""Compare text responses and streaming latency across configured API profiles."""

import argparse
import concurrent.futures
import datetime as dt
import json
import os
from pathlib import Path
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


PROTOCOLS = {"openai-responses", "openai-chat", "anthropic-messages", "gemini-generate-content"}


def parse_target(value):
    profile, separator, alias = value.partition(":")
    if not separator or not profile.strip() or not alias.strip():
        raise argparse.ArgumentTypeError("模型格式须为 配置档案名:模型别名，例如 openai:main")
    return profile.strip(), alias.strip()


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    profiles = config.get("providers") if isinstance(config, dict) else None
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("配置文件须有非空的 providers 对象")
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise ValueError(f"档案 {name} 须为对象")
        protocol = profile.get("protocol")
        if protocol not in PROTOCOLS:
            raise ValueError(f"档案 {name} 的 protocol 无效；支持：{', '.join(sorted(PROTOCOLS))}")
        base_url = profile.get("base_url")
        parsed = urllib.parse.urlparse(base_url) if isinstance(base_url, str) else None
        if not parsed or parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError(f"档案 {name} 的 base_url 须为不带查询参数的 HTTP(S) 地址")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError(f"档案 {name} 的远程 base_url 必须使用 HTTPS")
        if bool(profile.get("api_key_env")) == bool(profile.get("api_key")):
            raise ValueError(f"档案 {name} 须且只能设置 api_key_env 或 api_key")
        key_field = "api_key_env" if profile.get("api_key_env") else "api_key"
        if not isinstance(profile[key_field], str):
            raise ValueError(f"档案 {name} 的 {key_field} 须为字符串")
        models = profile.get("models")
        if not isinstance(models, dict) or not models or any(
            not isinstance(alias, str) or not alias or not isinstance(model, str) or not model
            for alias, model in models.items()
        ):
            raise ValueError(f"档案 {name} 的 models 须为别名到 API 模型 ID 的非空映射")
    return profiles


def api_key_for(profile):
    if profile.get("api_key"):
        return profile["api_key"]
    name = profile["api_key_env"]
    key = os.environ.get(name)
    if not key:
        raise ValueError(f"缺少环境变量：{name}")
    return key


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("须为正整数")
    return number


def read_input(inline, file_path, label):
    if inline is not None and file_path is not None:
        raise ValueError(f"{label} 不能同时指定文本和文件")
    if file_path:
        return sys.stdin.read() if file_path == "-" else Path(file_path).read_text(encoding="utf-8")
    return inline or ""


def sse_events(response):
    """Yield JSON data from SSE frames, including a final frame without a blank line."""
    data_lines = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data_lines:
                data = "\n".join(data_lines)
                data_lines = []
                if data == "[DONE]":
                    return
                yield json.loads(data)
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    if data_lines:
        data = "\n".join(data_lines)
        if data != "[DONE]":
            yield json.loads(data)


def make_request(profile, model, key, prompt, system, max_tokens):
    protocol = profile["protocol"]
    base_url = profile["base_url"].rstrip("/")
    if protocol == "openai-responses":
        body = {"model": model, "input": prompt, "stream": True,
                "max_output_tokens": max_tokens, "store": False}
        if system:
            body["instructions"] = system
        url = f"{base_url}/responses"
        headers = {"Authorization": f"Bearer {key}"}
    elif protocol == "openai-chat":
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": True}
        url = f"{base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
    elif protocol == "anthropic-messages":
        body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens, "stream": True}
        if system:
            body["system"] = system
        url = f"{base_url}/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        safe_model = urllib.parse.quote(model, safe="-_.")
        url = f"{base_url}/models/{safe_model}:streamGenerateContent?alt=sse"
        headers = {"x-goog-api-key": key}
    headers.update({"Content-Type": "application/json", "Accept": "text/event-stream"})
    return urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST"
    )


def extract_event(protocol, event):
    """Return visible text delta, input tokens, output tokens, and error."""
    kind = event.get("type")
    if protocol == "openai-responses":
        if kind == "response.output_text.delta":
            return event.get("delta", ""), None, None, None
        if kind in ("response.completed", "response.incomplete", "response.failed"):
            response = event.get("response", {})
            usage = response.get("usage") or {}
            error = response.get("error") or response.get("incomplete_details")
            if kind != "response.completed" and not error:
                error = kind
            return "", usage.get("input_tokens"), usage.get("output_tokens"), error
        if kind == "error":
            return "", None, None, event.get("message") or event
    elif protocol == "openai-chat":
        if "error" in event:
            return "", None, None, event["error"]
        choices = event.get("choices") or []
        delta = (choices[0].get("delta") or {}).get("content", "") if choices else ""
        usage = event.get("usage") or {}
        return delta or "", usage.get("prompt_tokens"), usage.get("completion_tokens"), None
    elif protocol == "anthropic-messages":
        if kind == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                return delta.get("text", ""), None, None, None
        if kind == "message_start":
            usage = (event.get("message") or {}).get("usage") or {}
            return "", usage.get("input_tokens"), usage.get("output_tokens"), None
        if kind == "message_delta":
            usage = event.get("usage") or {}
            return "", None, usage.get("output_tokens"), None
        if kind == "error":
            return "", None, None, event.get("error") or event
    else:
        if "error" in event:
            return "", None, None, event["error"]
        parts = ((event.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        visible = "".join(part.get("text", "") for part in parts
                          if not part.get("thought", False))
        usage = event.get("usageMetadata") or {}
        return visible, usage.get("promptTokenCount"), usage.get("candidatesTokenCount"), None
    return "", None, None, None


def run_one(name, alias, profile, index, key, prompt, system, max_tokens, timeout):
    model = profile["models"][alias]
    protocol = profile["protocol"]
    record = {"provider": name, "alias": alias, "model": model, "protocol": protocol,
              "base_url": profile["base_url"], "run": index, "status": "ok",
              "ttft_ms": None, "total_ms": None, "input_tokens": None,
              "output_tokens": None, "text": "", "error": None}
    started = time.perf_counter()
    try:
        request = make_request(profile, model, key, prompt, system, max_tokens)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for event in sse_events(response):
                delta, input_tokens, output_tokens, error = extract_event(protocol, event)
                if input_tokens is not None:
                    record["input_tokens"] = input_tokens
                if output_tokens is not None:
                    record["output_tokens"] = output_tokens
                if error:
                    raise RuntimeError(str(error))
                if delta:
                    if record["ttft_ms"] is None:
                        record["ttft_ms"] = round((time.perf_counter() - started) * 1000)
                    record["text"] += delta
        if not record["text"]:
            record["status"] = "empty"
    except urllib.error.HTTPError as exc:
        record["status"] = "error"
        detail = exc.read(1500).decode("utf-8", errors="replace")
        record["error"] = f"HTTP {exc.code}: {detail}"
    except Exception as exc:
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["total_ms"] = round((time.perf_counter() - started) * 1000)
    return record


def format_ms(value):
    return "—" if value is None else f"{value:,.0f}"


def fenced(text):
    longest = max((len(item) for item in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def report_markdown(data):
    records = data["results"]
    lines = ["# 模型对比报告", "", f"时间：{data['created_at']}", "",
             "## 汇总", "",
             "| 档案 | 模型别名 / ID | 协议 | 成功/总数 | 首字延迟中位数 (ms) | 总耗时中位数 (ms) | 输出 token 中位数 |",
             "| --- | --- | --- | ---: | ---: | ---: | ---: |"]
    for target in data["models"]:
        provider, alias, model = target["provider"], target["alias"], target["model"]
        group = [r for r in records if r["provider"] == provider and r["alias"] == alias]
        successful = [r for r in group if r["status"] == "ok"]
        def median(field):
            values = [r[field] for r in successful if r[field] is not None]
            return format_ms(statistics.median(values)) if values else "—"
        lines.append(f"| {provider} | {alias} / {model} | {target['protocol']} | {len(successful)}/{len(group)} | "
                     f"{median('ttft_ms')} | {median('total_ms')} | {median('output_tokens')} |")
    lines += ["", "## 每次回答", ""]
    for r in records:
        lines += [f"### {r['provider']} / {r['alias']} ({r['model']}) / 第 {r['run']} 次", "",
                  f"接口：{r['base_url']}；协议：{r['protocol']}", "",
                  f"状态：{r['status']}；首字延迟：{format_ms(r['ttft_ms'])} ms；"
                  f"总耗时：{format_ms(r['total_ms'])} ms；"
                  f"输入/输出 token：{r['input_tokens'] if r['input_tokens'] is not None else '—'}/"
                  f"{r['output_tokens'] if r['output_tokens'] is not None else '—'}", ""]
        if r["error"]:
            lines += [f"错误：{r['error']}", ""]
        if r["text"]:
            lines += [fenced(r["text"]), ""]
    lines += ["## 说明", "",
              "首字延迟是从本机发起 HTTP 请求到收到第一段可见文本的时间；总耗时是收到流结束的时间。"
              "网络、排队、缓存、推理方式和输出长度都会影响结果。"
              "不同厂商的 token 计数口径不完全一致；内容质量需要人工判断。", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="按供应商配置档案并发比较模型文本响应")
    parser.add_argument("--config", default="model-compare.json", help="供应商 JSON 配置文件")
    prompt_group = parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt", help="提示词文本")
    prompt_group.add_argument("--prompt-file", help="UTF-8 提示词文件；- 表示标准输入")
    parser.add_argument("--context-file", help="UTF-8 上下文文件，作为同一用户消息发送")
    parser.add_argument("--system", help="系统指令文本")
    parser.add_argument("--system-file", help="UTF-8 系统指令文件")
    parser.add_argument("--model", action="append", type=parse_target, required=True,
                        help="可重复，格式 档案名:模型别名，例如 openai:main")
    parser.add_argument("--runs", type=positive_int, default=1, help="每个模型重复次数，默认 1")
    parser.add_argument("--max-output-tokens", type=positive_int, default=1024)
    parser.add_argument("--timeout", type=positive_int, default=180, help="单次网络读取超时秒数")
    parser.add_argument("--out-dir", default="model-compare-results", help="输出目录")
    args = parser.parse_args(argv)
    try:
        profiles = load_config(args.config)
        prompt = read_input(args.prompt, args.prompt_file, "提示词")
        system = read_input(args.system, args.system_file, "系统指令")
        context = Path(args.context_file).read_text(encoding="utf-8") if args.context_file else ""
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    if not prompt.strip():
        parser.error("提示词不能为空")
    if context:
        prompt = f"<context>\n{context}\n</context>\n\n{prompt}"
    selected = list(dict.fromkeys(args.model))
    models = []
    keys = {}
    for name, alias in selected:
        profile = profiles.get(name)
        if profile is None:
            parser.error(f"配置中没有供应商档案：{name}")
        if alias not in profile["models"]:
            parser.error(f"档案 {name} 中没有模型别名：{alias}")
        try:
            keys[name] = api_key_for(profile)
        except ValueError as exc:
            parser.error(str(exc))
        models.append({"provider": name, "alias": alias, "model": profile["models"][alias],
                       "protocol": profile["protocol"], "base_url": profile["base_url"]})
    created_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    data = {"created_at": created_at, "prompt": prompt, "system": system,
            "models": models, "runs": args.runs, "max_output_tokens": args.max_output_tokens,
            "results": []}
    jobs = [(name, alias, i) for i in range(1, args.runs + 1) for name, alias in selected]
    print(f"发送 {len(jobs)} 个请求，目标 {len(models)} 个模型…", file=sys.stderr)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(jobs), 16)) as pool:
        futures = [pool.submit(run_one, name, alias, profiles[name], i, keys[name], prompt, system,
                               args.max_output_tokens, args.timeout) for name, alias, i in jobs]
        for future in concurrent.futures.as_completed(futures):
            r = future.result()
            data["results"].append(r)
            print(f"{r['provider']}:{r['alias']} #{r['run']} {r['status']} "
                  f"首字 {format_ms(r['ttft_ms'])} ms / 总计 {format_ms(r['total_ms'])} ms",
                  file=sys.stderr)
    order = {(name, alias, i): n for n, (name, alias, i) in enumerate(jobs)}
    data["results"].sort(key=lambda r: order[(r["provider"], r["alias"], r["run"])])
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    json_path, md_path = out / f"compare-{stamp}.json", out / f"compare-{stamp}.md"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(report_markdown(data), encoding="utf-8")
    print(f"JSON: {json_path}\n报告: {md_path}")
    return 1 if any(r["status"] != "ok" for r in data["results"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
