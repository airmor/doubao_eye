"""豆包网页版协议层：免登录上传 + 带附件对话。

所有网络请求都在豆包页面上下文里用 ``window.fetch`` 发出，由豆包前端
hook 自动注入 ``a_bogus`` 签名 —— 这是 2026 年新版反代防护下唯一可行
的通道（httpx 直连 / X-Bogus 旧签名已全部失效）。

上传流程（实测验证）：
  1. POST /alice/resource/prepare_upload      → 匿名 STS 凭证
  2. GET  /top/v1?Action=ApplyImageUpload      → 上传地址（AWS V4 签名，
     在浏览器内用 crypto.subtle 完成）
  3. POST https://{tos_host}/upload/v1/{uri}   → 直传 TOS
  4. POST /top/v1?Action=CommitImageUpload     → 提交确认

对话流程：POST /chat/completion（SSE 流式返回，新事件格式
STREAM_MSG_NOTIFY / STREAM_CHUNK / SSE_ACK / SSE_REPLY_END）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from .browser import CdpBrowser

log = logging.getLogger(__name__)

# 豆包前端硬编码的 ImageX 服务 ID（实测可用，勿改）
_IMAGE_SERVICE_ID = "a9rns2rl98"
_DEFAULT_BOT_ID = "7338286299411103781"

# ── 页面内 JS：公共工具 ──────────────────────────────────────

_JS_COMMON = """
  const enc = new TextEncoder();
  const hex = b => [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, '0')).join('');
  const b64ToBytes = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  async function hmac(key, msg) {
    const k = await crypto.subtle.importKey('raw', key,
      {name: 'HMAC', hash: 'SHA-256'}, false, ['sign']);
    return new Uint8Array(await crypto.subtle.sign('HMAC', k, enc.encode(msg)));
  }
  const sha256Hex = async d => hex(await crypto.subtle.digest(
    'SHA-256', typeof d === 'string' ? enc.encode(d) : d));

  // 设备参数：从页面 localStorage / cookie 取（与真实流量一致）
  let device_id = '', web_id = '';
  try { device_id = JSON.parse(localStorage.getItem('samantha_web_web_id') || '{}').web_id || ''; } catch (e) {}
  try { web_id = JSON.parse(localStorage.getItem('__tea_cache_tokens_497858') || '{}').web_id || ''; } catch (e) {}
  const fpM = document.cookie.match(/s_v_web_id=([^;]+)/);
  const fp = fpM ? fpM[1] : '';
  const baseParams = {
    version_code: '20800', language: 'zh', device_platform: 'web',
    doubao_device_platform: 'web', aid: '497858', real_aid: '497858',
    pkg_type: 'release_version', device_id,
    pc_version: '3.42.2', doubao_pc_version: '3.42.2',
    web_id, tea_uuid: web_id, region: 'CN', sys_region: 'CN',
    samantha_web: '1', web_platform: 'browser',
    'use-olympus-account': '1', web_tab_id: crypto.randomUUID(),
  };
  if (fp) baseParams.fp = fp;
  const qs = extra => new URLSearchParams({...baseParams, ...(extra || {})}).toString();
"""

# ── 页面内 JS：文件上传（4 步 TOS 流程）──────────────────────

_JS_UPLOAD = """
  // AWS Signature V4（使用 prepare_upload 返回的匿名 STS 凭证）
  async function awsSign(method, url, bodyBytes, tok) {
    const sp = [...url.searchParams.entries()]
      .sort((a, b) => (a[0] < b[0] ? -1 : (a[0] > b[0] ? 1 : 0)));
    const cqs = sp.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join('&');
    const now = new Date();
    const amzDate = now.toISOString().replace(/[-:]/g, '').replace(/\\.\\d+/, '');
    const dateStamp = amzDate.slice(0, 8);
    const hdrs = {host: url.host, 'x-amz-date': amzDate,
                  'x-amz-security-token': tok.session_token};
    const signed = Object.keys(hdrs).sort();
    const canonicalH = signed.map(k => `${k}:${hdrs[k]}\\n`).join('');
    const payloadHash = await sha256Hex(bodyBytes || new Uint8Array(0));
    const cr = `${method}\\n${url.pathname}\\n${cqs}\\n${canonicalH}\\n${signed.join(';')}\\n${payloadHash}`;
    const scope = `${dateStamp}/cn-north-1/imagex/aws4_request`;
    const crHash = await sha256Hex(cr);
    const sts = `AWS4-HMAC-SHA256\\n${amzDate}\\n${scope}\\n${crHash}`;
    const kD = await hmac(enc.encode('AWS4' + tok.secret_key), dateStamp);
    const kR = await hmac(kD, 'cn-north-1');
    const kS = await hmac(kR, 'imagex');
    const kSign = await hmac(kS, 'aws4_request');
    const sig = hex(await hmac(kSign, sts));
    return {
      'x-amz-date': amzDate,
      'x-amz-security-token': tok.session_token,
      'x-amz-content-sha256': payloadHash,
      'Authorization': `AWS4-HMAC-SHA256 Credential=${tok.access_key}/${scope}, `
        + `SignedHeaders=${signed.join(';')}, Signature=${sig}`,
    };
  }

  try {
    const fileBytes = b64ToBytes(arg.b64);
    const fileSize = fileBytes.length;
    if (!fileSize) return {error: '空文件'};

    // CRC32（TOS 上传必需）
    let crc = 0xFFFFFFFF;
    for (let i = 0; i < fileBytes.length; i++) {
      crc ^= fileBytes[i];
      for (let j = 0; j < 8; j++) crc = (crc >>> 1) ^ (0xEDB88320 & -(crc & 1));
    }
    const crc32Hex = ((crc ^ 0xFFFFFFFF) >>> 0).toString(16).padStart(8, '0');

    // Step 1: 匿名 STS 凭证（a_bogus 由站点 fetch hook 注入）
    const pr = await window.fetch('/alice/resource/prepare_upload?' + qs(), {
      method: 'POST',
      headers: {'content-type': 'application/json', 'agw-js-conv': 'str'},
      body: JSON.stringify({tenant_id: '5', scene_id: '5', resource_type: 1}),
      credentials: 'include',
    });
    const prep = await pr.json();
    if (prep.code !== 0) {
      return {error: 'prepare_upload 失败: ' + (prep.msg || JSON.stringify(prep).slice(0, 200))};
    }
    const tok = prep.data.upload_auth_token;
    // service_id：文件走 prepare_upload 返回的文件服务（如 ik7evvg4ik，
    // 服务端解析其内容）；图片沿用 UI 硬编码的图片服务 a9rns2rl98
    const serviceId = arg.isImage ? arg.serviceId : (prep.data.service_id || arg.serviceId);

    // Step 2: 申请上传位
    const applyPath = `/top/v1?Action=ApplyImageUpload&Version=2018-08-01`
      + `&ServiceId=${serviceId}&NeedFallback=true`
      + `&FileSize=${fileSize}&FileExtension=${arg.ext}&s=jdnfglwfkl`;
    const applyH = await awsSign('GET', new URL(applyPath, location.origin), null, tok);
    const ar = await window.fetch(applyPath, {method: 'GET', headers: applyH, credentials: 'include'});
    const aj = await ar.json();
    const addr = aj && aj.Result && aj.Result.UploadAddress;
    if (!addr || !addr.StoreInfos || !addr.StoreInfos.length) {
      return {error: 'ApplyImageUpload 失败: ' + JSON.stringify(aj).slice(0, 250)};
    }
    const store = addr.StoreInfos[0];
    const tosHost = (addr.UploadHosts || [])[0] || 'tos-lf-x.snssdk.com';

    // Step 3: 直传 TOS（跨域，浏览器自动处理 preflight）
    const tr = await window.fetch(`https://${tosHost}/upload/v1/${store.StoreUri}`, {
      method: 'POST',
      headers: {
        'Authorization': store.Auth,
        'Content-CRC32': crc32Hex,
        'Content-Type': 'application/octet-stream',
      },
      body: fileBytes,
    });
    const tj = await tr.json();
    if (tj.code !== 2000) {
      return {error: 'TOS 上传失败: ' + JSON.stringify(tj).slice(0, 200)};
    }

    // Step 4: 提交确认
    const commitBody = JSON.stringify({SessionKey: addr.SessionKey});
    const commitPath = `/top/v1?Action=CommitImageUpload&Version=2018-08-01&ServiceId=${serviceId}`;
    const ch = await awsSign('POST', new URL(commitPath, location.origin), enc.encode(commitBody), tok);
    ch['Content-Type'] = 'application/json';
    const cr2 = await window.fetch(commitPath, {
      method: 'POST', headers: ch, body: commitBody, credentials: 'include',
    });
    const cj = await cr2.json();
    const st = cj && cj.Result && cj.Result.Results && cj.Result.Results[0];
    if (!st || st.UriStatus !== 2000) {
      return {error: 'CommitImageUpload 失败: ' + JSON.stringify(cj).slice(0, 250)};
    }
    return {ok: true, uri: store.StoreUri, size: fileSize};
  } catch (e) {
    return {error: '上传异常: ' + (e && e.message ? e.message : String(e)).slice(0, 200)};
  }
"""

# ── 页面内 JS：带附件对话（SSE 流式）─────────────────────────

_JS_CHAT = """
  // 文件解析预处理：豆包后端异步解析文件（txt/pdf/docx 等必需，否则
  // 发送时服务端读不到内容）。pre_handle 成功后再继续。
  try {
    if (arg.files && arg.files.length) {
      const php = new URLSearchParams({
        version_code: '20800', language: 'zh', device_platform: 'web',
        doubao_device_platform: 'web', aid: '497858', real_aid: '497858',
        pkg_type: 'release_version', device_id,
        pc_version: '3.42.2', doubao_pc_version: '3.42.2',
        web_id, tea_uuid: web_id, region: 'CN', sys_region: 'CN',
        samantha_web: '1', web_platform: 'browser',
        'use-olympus-account': '1', web_tab_id: crypto.randomUUID(),
      });
      for (const f of arg.files) {
        const preBody = JSON.stringify({
          uplink_entity: {
            entity_type: 2,
            entity_content: {file: {
              key: f.uri,
              file_name: f.name || 'file',
              file_size: f.size || 0,
              file_type: 0,
            }},
            identifier: crypto.randomUUID(),
          },
          conversation_id: '',
          bot_id: arg.botId,
          section_id: '',
          local_message_id: crypto.randomUUID(),
        });
        let preOk = false;
        for (let i = 0; i < 3 && !preOk; i++) {
          try {
            const pr = await window.fetch('/alice/message/pre_handle_v2?' + php.toString(), {
              method: 'POST',
              headers: {'Content-Type': 'application/json', 'agw-js-conv': 'str'},
              body: preBody,
              credentials: 'include',
            });
            const pj = await pr.json().catch(() => ({}));
            if (pj.code === 0) preOk = true;
            else if (i === 2) {
              return {error: '文件解析失败(pre_handle_v2): '
                + JSON.stringify(pj).slice(0, 200)};
            }
          } catch (e) {
            if (i === 2) return {error: '文件解析请求异常: ' + (e.message || String(e))};
          }
          if (!preOk) await new Promise(r => setTimeout(r, 1500));
        }
      }
    }
  } catch (e) {}

  // 文本累积：兼容 delta 与 cumulative 两种事件形态
  let acc = '';
  const push = t => {
    if (!t) return;
    if (t.startsWith(acc)) acc = t;           // cumulative 快照
    else if (!acc.endsWith(t)) acc += t;       // 增量 delta
  };

  try {
    // 附件与文本按豆包 UI 真实结构分两条 message 发送（附件在前）。
    // 实测：合并成一条 message 的多个 block 时，服务端读不到文件内容。
    const msgs = [];
    if (arg.files && arg.files.length) {
      msgs.push({
        local_message_id: crypto.randomUUID(),
        content_block: [{
          block_type: 10052,
          content: {attachment_block: {attachments: arg.files.map(f => ({
            type: 3, identifier: crypto.randomUUID(),
            file: {uri: f.uri, url: '', file_type: 0, name: f.name, size: f.size},
            parse_state: 1, review_state: 1, upload_status: 1, progress: 100, src: '',
          }))}, pc_event_block: ''},
          block_id: crypto.randomUUID(), parent_id: '', meta_info: [],
          extra_info: {}, append_fields: [],
        }],
        message_status: 0,
      });
    }
    if (arg.images && arg.images.length) {
      // 多图合并进同一条附件 message（attachments 为数组，与 UI 结构一致）
      msgs.push({
        local_message_id: crypto.randomUUID(),
        content_block: [{
          block_type: 10052,
          content: {attachment_block: {attachments: arg.images.map(img => ({
            type: 1, identifier: crypto.randomUUID(),
            image: {
              name: img.name, uri: img.uri,
              image_ori: {url: '', width: img.width || 64, height: img.height || 64,
                          format: img.format || 'png', url_formats: {}},
            },
            parse_state: 0, review_state: 1, upload_status: 1, progress: 100, src: '',
          }))}, pc_event_block: ''},
          block_id: crypto.randomUUID(), parent_id: '', meta_info: [],
          extra_info: {}, append_fields: [], is_finish: true, patch_type: 2,
        }],
        message_status: 0,
      });
    }
    msgs.push({
      local_message_id: crypto.randomUUID(),
      content_block: [{
        block_type: 10000,
        content: {text_block: {text: arg.text, icon_url: '', icon_url_dark: '', summary: ''},
                  pc_event_block: ''},
        block_id: crypto.randomUUID(), parent_id: '', meta_info: [],
        extra_info: {}, append_fields: [],
      }],
      message_status: 0,
    });

    const now = Date.now();
    const payload = {
      client_meta: {
        local_conversation_id: 'local_' + now,
        conversation_id: '',
        bot_id: arg.botId,
        last_section_id: '',
        last_message_index: null,
      },
      messages: msgs,
      option: {
        send_message_scene: '', create_time_ms: now, collect_id: '',
        is_audio: false, answer_with_suggest: false, agent_mode: 2,
        tts_switch: false, need_deep_think: 0, click_clear_context: false,
        from_suggest: false, is_regen: false, is_replace: false,
        disable_sse_cache: false, select_text_action: '', resend_for_regen: false,
        scene_type: 0, unique_key: crypto.randomUUID(), start_seq: 0,
        need_create_conversation: true, regen_query_id: [], edit_query_id: [],
        regen_instruction: '', no_replace_for_regen: false, message_from: 0,
        shared_app_name: '', shared_app_id: '',
        sse_recv_event_options: {support_chunk_delta: true},
        is_ai_playground: false, message_storage_type: 0,
      },
      ext: {
        agent_mode: '2', use_deep_think: '0', collection_id: '',
        commerce_credit_config_enable: '0', sub_conv_firstmet_type: '1',
      },
    };

    const params = new URLSearchParams({
      aid: '497858', device_id, device_platform: 'web',
      doubao_device_platform: 'web', fp: fp || '', language: 'zh',
      pc_version: '3.42.2', doubao_pc_version: '3.42.2',
      pkg_type: 'release_version', real_aid: '497858', region: 'CN',
      samantha_web: '1', sys_region: 'CN', tea_uuid: web_id,
      tz_name: 'Asia/Shanghai', 'use-olympus-account': '1',
      version_code: '20800', web_id, web_platform: 'browser',
      web_tab_id: crypto.randomUUID(),
    });

    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), arg.timeoutMs);
    let resp;
    try {
      resp = await window.fetch('/chat/completion?' + params.toString(), {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'agw-js-conv': 'str'},
        body: JSON.stringify(payload),
        credentials: 'include',
        signal: ctrl.signal,
      });
    } catch (e) {
      clearTimeout(timer);
      return {error: '网络请求失败: ' + (e && e.name === 'AbortError'
        ? '等待回答超时(' + Math.round(arg.timeoutMs / 1000) + 's)'
        : (e.message || String(e)))};
    }
    if (!resp.ok) {
      clearTimeout(timer);
      const t = await resp.text().catch(() => '');
      return {error: 'HTTP ' + resp.status + ': ' + t.slice(0, 300)};
    }
    const ctype = (resp.headers.get('content-type') || '').toLowerCase();
    if (ctype.includes('application/json')) {
      clearTimeout(timer);
      const j = await resp.json().catch(() => ({}));
      return {error: '接口错误: ' + JSON.stringify(j).slice(0, 300)};
    }

    // 解析 SSE
    let convId = '';
    let brief = '';
    let firstError = '';
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    let evt = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      const lines = buf.split('\\n');
      buf = lines.pop();
      for (const line of lines) {
        const t = line.trim();
        if (!t) continue;
        if (t.startsWith('event:')) { evt = t.slice(6).trim(); continue; }
        if (!t.startsWith('data:')) continue;
        const dataStr = t.slice(5).trim();
        if (!dataStr || dataStr === '{}') continue;
        let obj;
        try { obj = JSON.parse(dataStr); } catch (e) { continue; }
        const e2 = evt || '';
        if (obj.error_code && obj.error_code !== 0 && !firstError) {
          firstError = 'error_code=' + obj.error_code + ' ' + (obj.error_msg || obj.message || '');
        }
        if (e2 === 'SSE_ACK' && obj.ack_client_meta && obj.ack_client_meta.conversation_id) {
          convId = obj.ack_client_meta.conversation_id;
        }
        if (e2 === 'SSE_REPLY_END' && obj.brief) brief = String(obj.brief);
        if (e2 === 'CHUNK_DELTA' && typeof obj.text === 'string') push(obj.text);
        if (Array.isArray(obj.patch_op)) {
          for (const op of obj.patch_op) {
            const pv = op.patch_value || {};
            for (const b of (pv.content_block || [])) {
              const tb = (b.content || {}).text_block || {};
              if (tb.text) push(tb.text);
            }
            if (op.patch_object === 102 && typeof pv.content === 'string' && pv.content) {
              try { const p = JSON.parse(pv.content); if (p && p.text) push(p.text); } catch (e) {}
            }
          }
        }
        if (e2 === 'STREAM_MSG_NOTIFY' && obj.content && Array.isArray(obj.content.content_block)) {
          for (const b of obj.content.content_block) {
            const tb = (b.content || {}).text_block || {};
            if (tb.text) push(tb.text);
          }
        }
        evt = '';
      }
    }
    clearTimeout(timer);
    if (firstError && !acc) return {error: '豆包返回错误: ' + firstError.slice(0, 250)};
    if (!acc && brief) acc = brief;   // 兜底：SSE_REPLY_END 里的摘要
    if (!acc) return {error: '未收到回答（流为空）'};
    return {ok: true, text: acc, conversation_id: convId};
  } catch (e) {
    return {error: '对话异常: ' + (e && e.message ? e.message : String(e)).slice(0, 200)};
  }
"""


class DoubaoSession:
    """在一个 CdpBrowser 页面上执行上传 / 对话。"""

    def __init__(self, browser: CdpBrowser, bot_id: str = _DEFAULT_BOT_ID,
                 chat_timeout: float = 150.0):
        self.browser = browser
        self.bot_id = bot_id
        self.chat_timeout = chat_timeout

    # ── 上传 ──────────────────────────────────────────────────

    async def upload(self, data: bytes, filename: str, is_image: bool = False,
                     timeout: float = 300.0) -> dict:
        """上传 bytes 到豆包 TOS，返回 ``{"uri": ..., "size": ...}``。

        Args:
            is_image: True 走图片服务（a9rns2rl98）；False 走
                prepare_upload 返回的文件服务（服务端会解析文件内容）。
        """
        import base64
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if not ext:
            ext = "bin"
        arg = {
            "b64": base64.b64encode(data).decode("ascii"),
            "ext": f".{ext}",
            "serviceId": _IMAGE_SERVICE_ID,
            "isImage": bool(is_image),
        }
        js = "(async (arg) => {" + _JS_COMMON + _JS_UPLOAD + "})(" \
             + json.dumps(arg) + ")"
        result = await self.browser.evaluate(js, timeout=timeout)
        if not isinstance(result, dict) or result.get("error"):
            raise RuntimeError(f"上传失败: {(result or {}).get('error', '未知错误')}")
        log.info("上传成功: %s -> %s (%d bytes)", filename,
                 result.get("uri", "")[:60], result.get("size", 0))
        return result

    # ── 对话 ──────────────────────────────────────────────────

    async def chat(
        self,
        text: str,
        images: Optional[list] = None,
        files: Optional[list] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        """发送带附件的消息，收集完整回答。

        Args:
            text: 用户问题。
            images: 图片引用列表 ``[{uri, name, width, height, format}]``。
            files: 文件引用列表 ``[{uri, name, size}]``。
        Returns:
            ``{"text": 回答, "conversation_id": 会话id}``
        """
        arg = {
            "text": text,
            "images": images or [],
            "files": files or [],
            "botId": self.bot_id,
            "timeoutMs": int((timeout or self.chat_timeout) * 1000),
        }
        js = "(async (arg) => {" + _JS_COMMON + _JS_CHAT + "})(" \
             + json.dumps(arg) + ")"
        started = time.monotonic()
        result = await self.browser.evaluate(
            js, timeout=(timeout or self.chat_timeout) + 30
        )
        elapsed = time.monotonic() - started
        if not isinstance(result, dict) or result.get("error"):
            raise RuntimeError(
                f"对话失败: {(result or {}).get('error', '未知错误')}"
            )
        log.info("豆包回答完成（%.1fs, %d 字符）", elapsed, len(result.get("text", "")))
        return result
