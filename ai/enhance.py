import os
import json
import sys
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict
from queue import Queue
from threading import Lock
import dotenv
import argparse
import requests
from tqdm import tqdm

from pydantic import ValidationError
import langchain_core.exceptions
from langchain_openai import ChatOpenAI
from langchain.prompts import (
    ChatPromptTemplate,
    SystemMessagePromptTemplate,
    HumanMessagePromptTemplate,
)
from structure import Structure
from content_filter import is_sensitive
from runtime import (
    build_chat_openai_kwargs,
    invoke_with_retry,
    describe_failure,
    retry_config,
    raise_if_processing_failed,
)

if os.path.exists('.env'):
    dotenv.load_dotenv()
template = open("template.txt", "r").read()
system = open("system.txt", "r").read()

def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=str, required=True, help="jsonline data file")
    parser.add_argument("--max_workers", type=int, default=1, help="Maximum number of parallel workers")
    return parser.parse_args()

# A backslash inside a JSON string may only start one of these escapes.
# Anything else (e.g. LaTeX \mathcal) is invalid JSON and must be repaired.
_VALID_JSON_ESCAPES = set('"\\/bfnrtu')

_TRAILING_COMMA_RE = re.compile(r',(\s*[}\]])')


def _extract_json_object_text(text: str) -> str:
    """Return the outermost {...} span, tolerating prose before/after it.

    Scanning for a balanced object (rather than trusting LangChain's exact
    phrasing) means a model that wraps its JSON in commentary, or nests
    objects inside strings, is still recovered in full instead of being
    truncated at the first brace.
    """
    start = text.find('{')
    if start == -1:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    # Unbalanced (response truncated mid-object): return the remainder so the
    # caller can still attempt a repair instead of losing everything.
    return text[start:]


def _repair_backslash_escapes(text: str) -> str:
    """Escape only the backslashes that are not already valid JSON escapes.

    The model writes LaTeX such as \\mathcal or \\alpha with a single
    backslash. Those are invalid JSON escapes, so they must be doubled -- but
    a blanket ``replace('\\\\', '\\\\\\\\')`` also corrupts escapes that were
    already correct (``\\\\``, ``\\"``, ``\\n``), which is exactly what made
    the original recovery path fail on real abstracts and publish placeholders.
    """
    out = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != '\\':
            out.append(char)
            index += 1
            continue
        # A backslash at the very end cannot start a valid escape.
        following = text[index + 1] if index + 1 < length else ''
        if following in _VALID_JSON_ESCAPES:
            out.append(char)
            out.append(following)
            index += 2
            # \uXXXX needs its four hex digits consumed as-is.
            if following == 'u':
                digits = text[index:index + 4]
                if len(digits) == 4 and all(c in '0123456789abcdefABCDEF' for c in digits):
                    out.append(digits)
                    index += 4
            continue
        # Invalid escape: neutralise it by escaping the backslash itself.
        out.append('\\\\')
        index += 1
    return ''.join(out)


def recover_partial_ai_fields(error_msg: str) -> dict:
    """Best-effort salvage of the model's fields from an unparseable response.

    Pure and side-effect free: it takes the OutputParserException text and
    returns whatever fields could be recovered, or ``{}`` when nothing could
    be. It never raises, so a single malformed response degrades to the
    default placeholders instead of aborting the whole batch.

    Deliberately touches neither the network nor ChatOpenAI, so it is
    importable and testable without API keys or a live model.
    """
    if not error_msg:
        return {}

    # Prefer the region after LangChain's marker, but fall back to the whole
    # message so a differently-worded parser error is still recoverable.
    marker = "Function Structure arguments:"
    candidate = error_msg.split(marker, 1)[1] if marker in error_msg else error_msg
    candidate = candidate.split('are not valid JSON')[0]

    json_text = _extract_json_object_text(candidate)
    if not json_text:
        return {}

    for attempt in (
        lambda: json.loads(json_text),
        lambda: json.loads(_repair_backslash_escapes(json_text)),
        lambda: json.loads(
            _TRAILING_COMMA_RE.sub(r'\1', _repair_backslash_escapes(json_text))
        ),
    ):
        try:
            parsed = attempt()
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _unrecoverable_message(item_id: str, error: BaseException, source: str) -> str:
    """Message for a response whose fields could not be recovered at all.

    Publishing the default placeholder strings instead would render as a real
    summary on the site (to_md/convert.py checks key PRESENCE only), so an
    unrecoverable response must fail the paper -- the same contract
    raise_if_processing_failed enforces for every other failure.
    """
    return (
        f"{describe_failure(item_id, error)} "
        f"[{source}: recovered no fields, refusing to publish placeholder text]"
    )


def process_single_item(chain, item: Dict, language: str) -> Dict:
    def check_github_code(content: str) -> Dict:
        """提取并验证 GitHub 链接"""
        code_info = {}

        # 1. 优先匹配 github.com/owner/repo 格式
        github_pattern = r"https?://github\.com/([a-zA-Z0-9-_]+)/([a-zA-Z0-9-_\.]+)"
        match = re.search(github_pattern, content)
        
        if match:
            owner, repo = match.groups()
            # 清理 repo 名称，去掉可能的 .git 后缀或末尾的标点
            repo = repo.rstrip(".git").rstrip(".,)")
            
            full_url = f"https://github.com/{owner}/{repo}"
            code_info["code_url"] = full_url
            
            # 尝试调用 GitHub API 获取信息
            github_token = os.environ.get("TOKEN_GITHUB")
            headers = {"Accept": "application/vnd.github.v3+json"}
            if github_token:
                headers["Authorization"] = f"token {github_token}"
            
            try:
                api_url = f"https://api.github.com/repos/{owner}/{repo}"
                resp = requests.get(api_url, headers=headers, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    code_info["code_stars"] = data.get("stargazers_count", 0)
                    code_info["code_last_update"] = data.get("pushed_at", "")[:10]
            except Exception:
                # API 调用失败不影响主流程
                pass
            return code_info

        # 2. 如果没有 github.com，尝试匹配 github.io
        github_io_pattern = r"https?://[a-zA-Z0-9-_]+\.github\.io(?:/[a-zA-Z0-9-_\.]+)*"
        match_io = re.search(github_io_pattern, content)
        
        if match_io:
            url = match_io.group(0)
            # 清理末尾标点
            url = url.rstrip(".,)")
            code_info["code_url"] = url
            # github.io 不进行 star 和 update 判断
                
        return code_info

    # 检查 summary 字段
    if is_sensitive(item.get("summary", "")):
        return None

    # 检测代码可用性
    code_info = check_github_code(item.get("summary", ""))
    if code_info:
        item.update(code_info)

    """处理单个数据项"""
    # Default structure with meaningful fallback values
    default_ai_fields = {
        "tldr": "Summary generation failed",
        "motivation": "Motivation analysis unavailable",
        "method": "Method extraction failed",
        "result": "Result analysis unavailable",
        "conclusion": "Conclusion extraction failed"
    }
    
    try:
        # Transient provider failures (HTTP 429, 5xx, timeouts, connection
        # resets, "overloaded") are expected when 8 workers hit ~1000 papers.
        # invoke_with_retry retries those with bounded exponential backoff +
        # jitter and raises everything else (auth, quota, model-not-found,
        # context-length) immediately. A retried-but-successful call returns a
        # normal Structure, so no placeholder text is produced.
        response: Structure = invoke_with_retry(
            chain,
            {
                "language": language,
                "content": item['summary']
            },
            on_retry=lambda attempt, delay, error: print(
                f"Retry {attempt} for {item.get('id', 'unknown')} in {delay:.1f}s "
                f"after {type(error).__name__}: {error}",
                file=sys.stderr,
            ),
        )
        item['AI'] = response.model_dump()
    except langchain_core.exceptions.OutputParserException as e:
        # The model emitted something that is not valid JSON -- typically an
        # abstract containing LaTeX such as \mathcal, whose single backslash is
        # an invalid JSON escape. Salvage whatever fields we can (see
        # recover_partial_ai_fields).
        partial_data = recover_partial_ai_fields(str(e))

        if not partial_data:
            # Nothing readable: every field would be a placeholder string, which
            # to_md/convert.py renders as a real summary. Fail the paper instead.
            message = _unrecoverable_message(
                item.get('id', 'unknown'), e, "OutputParserException"
            )
            print(f"Unrecoverable response: {message}", file=sys.stderr)
            raise RuntimeError(message) from e

        # Merge partial data with defaults to ensure all fields exist
        item['AI'] = {**default_ai_fields, **partial_data}
        print(f"Using partial AI data for {item.get('id', 'unknown')}: {list(partial_data.keys())}", file=sys.stderr)
    except ValidationError as e:
        # The model returned valid JSON but omitted required fields -- typically
        # because generation was truncated on a very long abstract. Recover the
        # fields it did produce and fill the rest with defaults, rather than
        # discarding the whole (expensive) batch over one bad response.
        #
        # Note: only str(e) truncates long values; e.errors()[i]["input"] holds
        # the full untruncated dict, so read from there.
        partial_data = {}
        for error in e.errors():
            value = error.get("input")
            if isinstance(value, dict):
                partial_data.update(value)

        if not partial_data:
            # No usable input on any error: the response carried nothing we can
            # publish, so treat it as failed rather than emitting placeholders.
            message = _unrecoverable_message(
                item.get('id', 'unknown'), e, "ValidationError"
            )
            print(f"Unrecoverable response: {message}", file=sys.stderr)
            raise RuntimeError(message) from e

        item['AI'] = {**default_ai_fields, **partial_data}
        missing = [f for f in default_ai_fields if f not in partial_data]
        print(
            f"Model omitted {missing} for {item.get('id', 'unknown')}; "
            f"recovered {list(partial_data.keys())} (abstract may be too long)",
            file=sys.stderr,
        )
    except Exception as e:
        # Actionable message: paper id + exception type + short diagnosis
        # (e.g. "rate limited after 4 retries" vs "auth failure"). The
        # `N paper(s) failed` contract in raise_if_processing_failed is
        # unchanged.
        message = describe_failure(item.get('id', 'unknown'), e)
        print(f"Unexpected error: {message}", file=sys.stderr)
        raise RuntimeError(message) from e
    
    # Final validation to ensure all required fields exist
    for field in default_ai_fields.keys():
        if field not in item['AI']:
            item['AI'][field] = default_ai_fields[field]

    # 检查 AI 生成的所有字段
    for v in item.get("AI", {}).values():
        if is_sensitive(str(v)):
            return None
    return item

def process_all_items(data: List[Dict], model_name: str, language: str, max_workers: int) -> List[Dict]:
    """并行处理所有数据项"""
    # Cap output length so a long abstract cannot cause a truncated (and thus
    # schema-invalid) response. Overridable without a code change.
    max_tokens_env = os.environ.get("MAX_TOKENS", "").strip()
    extra = {}
    if max_tokens_env:
        try:
            extra["max_tokens"] = int(max_tokens_env)
        except ValueError:
            print(
                f"Ignoring invalid MAX_TOKENS={max_tokens_env!r}",
                file=sys.stderr,
            )
    else:
        extra["max_tokens"] = 4096

    llm = ChatOpenAI(
        **build_chat_openai_kwargs(
            model_name=model_name,
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.environ.get("OPENAI_API_KEY", ""),
        ),
        **extra,
    ).with_structured_output(Structure, method="function_calling")

    print('Connect to:', model_name, file=sys.stderr)
    
    prompt_template = ChatPromptTemplate.from_messages([
        SystemMessagePromptTemplate.from_template(system),
        HumanMessagePromptTemplate.from_template(template=template)
    ])

    chain = prompt_template | llm

    # Retry policy is env-tunable without a code change (mirrors MAX_TOKENS):
    #   AI_MAX_RETRIES (default 3), AI_RETRY_BASE_DELAY (2s),
    #   AI_RETRY_MAX_DELAY (30s), AI_RETRY_JITTER (0.5).
    retry_settings = retry_config()
    print(
        "Retry policy: max_retries={max_retries} base_delay={base_delay}s "
        "max_delay={max_delay}s jitter={jitter}".format(**retry_settings),
        file=sys.stderr,
    )
    
    # 使用线程池并行处理
    processed_data = [None] * len(data)  # 预分配结果列表
    processing_errors = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # 提交所有任务
        future_to_idx = {
            executor.submit(process_single_item, chain, item, language): idx
            for idx, item in enumerate(data)
        }
        
        # 使用tqdm显示进度
        for future in tqdm(
            as_completed(future_to_idx),
            total=len(data),
            desc="Processing items"
        ):
            idx = future_to_idx[future]
            try:
                result = future.result()
                processed_data[idx] = result
            except Exception as e:
                print(f"Item at index {idx} generated an exception: {e}", file=sys.stderr)
                processing_errors.append(str(e))

    raise_if_processing_failed(processing_errors)
    
    return processed_data

def main():
    args = parse_args()
    model_name = os.environ.get("MODEL_NAME", "gpt-4o-mini")
    language = os.environ.get("LANGUAGE", 'Chinese')

    # 检查并删除目标文件
    target_file = args.data.replace('.jsonl', f'_AI_enhanced_{language}.jsonl')
    if os.path.exists(target_file):
        os.remove(target_file)
        print(f'Removed existing file: {target_file}', file=sys.stderr)

    # 读取数据
    data = []
    with open(args.data, "r") as f:
        for line in f:
            data.append(json.loads(line))

    # 去重
    seen_ids = set()
    unique_data = []
    for item in data:
        if item['id'] not in seen_ids:
            seen_ids.add(item['id'])
            unique_data.append(item)

    data = unique_data
    print('Open:', args.data, file=sys.stderr)
    
    # 并行处理所有数据
    processed_data = process_all_items(
        data,
        model_name,
        language,
        args.max_workers
    )
    
    # 保存结果
    with open(target_file, "w") as f:
        for item in processed_data:
            if item is not None:
                f.write(json.dumps(item) + "\n")

if __name__ == "__main__":
    main()
