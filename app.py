#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JustRunMy.app 自动续期(账号密码登录 + 物理点击 Turnstile 版)

相对原始脚本的改进:
  1. 续期成功判定改为「前后倒计时差值法」,不再写死 "1d 11" / "2 days"
  2. 登录结果判定改为「轮询是否离开登录页」,兼容带参/无尾斜杠/跳 /panel
  3. js_fill_input 用 json.dumps 生成 JS 字面量,彻底消除转义/注入 bug
  4. 物理点击加入 HiDPI(devicePixelRatio)坐标校准,可用 DPI_SCALE 覆盖
  5. Turnstile 弹窗等待、重试更稳;失败分支均有截图 + TG 通知

依赖:
  pip install seleniumbase requests
  Linux 物理点击需要: xdotool (apt install xdotool)
必需环境变量:
  JUSTRUNMY_EMAIL / JUSTRUNMY_PASSWORD
可选环境变量:
  TG_BOT_TOKEN / TG_CHAT_ID        通知
  IS_PROXY=true                    启用本地代理 http://127.0.0.1:1081
  PROXY_URL=http://127.0.0.1:1081  自定义代理地址
  DPI_SCALE=1.5                    强制指定屏幕缩放(默认自动读取 devicePixelRatio)
"""

import os
import re
import json
import time
import ctypes
import contextlib
import subprocess
import requests
from seleniumbase import SB

# ============================== 配置 ==============================
LOGIN_URL = "https://justrunmy.app/id/Account/Login"
PANEL_URL = "https://justrunmy.app/panel/applications"

EMAIL = os.environ.get("JUSTRUNMY_EMAIL") or ""
PASSWORD = os.environ.get("JUSTRUNMY_PASSWORD") or ""
TG_CHAT_ID = os.environ.get("TG_CHAT_ID") or ""
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""

# 续期后剩余时长至少增加这么多秒才算成功(默认 10 分钟,防抖动误判)
MIN_TIMER_INCREASE = int(os.environ.get("MIN_TIMER_INCREASE", "600"))


# ============================== 通知 ==============================
def send_tg_message(status_icon, status_text, time_left):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ℹ️ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID,跳过 Telegram 推送。")
        return

    local_time = time.gmtime(time.time() + 8 * 3600)
    current_time_str = time.strftime("%Y-%m-%d %H:%M:%S", local_time)

    # 邮箱脱敏
    if "@" in EMAIL:
        name, domain = EMAIL.split("@", 1)
        masked_email = (f"{name[:2]}****{name[-2:]}@{domain}"
                        if len(name) > 4 else f"{name}@{domain}")
    else:
        masked_email = (EMAIL[:2] + "****") if EMAIL else "未配置"

    # 时长格式化(如 "1d 11:59" / "11:59")
    def _short_time(s):
        if not s:
            return s
        m = re.search(r"(\d+)\s*(?:d|day|days)\b\s*([0-2]?\d:\d{2})", s, re.I)
        if m:
            return f"{m.group(1)}d {m.group(2)}"
        m = re.search(r"([0-2]?\d:\d{2})", s)
        return m.group(1) if m else s

    text = (
        f"🇩🇪 JustRunMy 续期通知\n\n"
        f"{status_icon} {status_text}\n"
        f"👤 账号: {masked_email}\n"
        f"⏱️ Expiration time: {_short_time(str(time_left))}\n"
        f"⏱️ Running time: {current_time_str}"
    )

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10)
        if r.status_code == 200:
            print("  📩 Telegram 通知发送成功!")
        else:
            print(f"  ⚠️ Telegram 通知发送失败: {r.text}")
    except Exception as e:
        print(f"  ⚠️ Telegram 通知发送异常: {e}")


# ============================== 注入用 JS ==============================
_EXPAND_JS = """
(function() {
    var ts = document.querySelector('input[name="cf-turnstile-response"]');
    if (!ts) return 'no-turnstile';
    var el = ts;
    for (var i = 0; i < 20; i++) {
        el = el.parentElement;
        if (!el) break;
        var s = window.getComputedStyle(el);
        if (s.overflow === 'hidden' || s.overflowX === 'hidden' || s.overflowY === 'hidden')
            el.style.overflow = 'visible';
        el.style.minWidth = 'max-content';
    }
    document.querySelectorAll('iframe').forEach(function(f){
        if (f.src && f.src.includes('challenges.cloudflare.com')) {
            f.style.width = '300px'; f.style.height = '65px';
            f.style.minWidth = '300px';
            f.style.visibility = 'visible'; f.style.opacity = '1';
        }
    });
    return 'done';
})()
"""

_EXISTS_JS = """
(function(){
    return document.querySelector('input[name="cf-turnstile-response"]') !== null;
})()
"""

_SOLVED_JS = """
(function(){
    var i = document.querySelector('input[name="cf-turnstile-response"]');
    return !!(i && i.value && i.value.length > 20);
})()
"""

_COORDS_JS = """
(function(){
    var iframes = document.querySelectorAll('iframe');
    for (var i = 0; i < iframes.length; i++) {
        var src = iframes[i].src || '';
        if (src.includes('cloudflare') || src.includes('turnstile') || src.includes('challenges')) {
            var r = iframes[i].getBoundingClientRect();
            if (r.width > 0 && r.height > 0)
                return {cx: Math.round(r.x + 30), cy: Math.round(r.y + r.height / 2)};
        }
    }
    var inp = document.querySelector('input[name="cf-turnstile-response"]');
    if (inp) {
        var p = inp.parentElement;
        for (var j = 0; j < 5; j++) {
            if (!p) break;
            var r = p.getBoundingClientRect();
            if (r.width > 100 && r.height > 30)
                return {cx: Math.round(r.x + 30), cy: Math.round(r.y + r.height / 2)};
            p = p.parentElement;
        }
    }
    return null;
})()
"""

# 新增 dpr(devicePixelRatio),用于 HiDPI 坐标换算
_WININFO_JS = """
(function(){
    return {
        sx: window.screenX || 0,
        sy: window.screenY || 0,
        oh: window.outerHeight,
        ih: window.innerHeight,
        dpr: window.devicePixelRatio || 1
    };
})()
"""


# ============================== 时长解析 ==============================
def normalize_timer_text(text: str) -> str:
    text = " ".join(text.split())
    m = re.search(r"\b\d+\s*(?:d|day|days)\b\s*\d{1,2}:\d{2}", text, re.I)
    if m:
        return m.group(0)
    m = re.search(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", text)
    return m.group(0) if m else text


def parse_timer_seconds(text):
    """把各种倒计时文案解析为总秒数;无法解析返回 None。

    支持: "1d 11:59" / "2 days 11:59:30" / "11:59" / "2 days 3 hours 4 minutes"
    """
    if not text:
        return None
    t = " ".join(str(text).split()).lower()

    # 形式一:含冒号 [dd?] HH:MM(:SS)
    m = re.search(
        r"(?:(\d+)\s*(?:d|day|days))?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?", t)
    if m:
        days = int(m.group(1) or 0)
        hh, mm, ss = int(m.group(2)), int(m.group(3)), int(m.group(4) or 0)
        return days * 86400 + hh * 3600 + mm * 60 + ss

    # 形式二:纯文字 "2 days 3 hours 4 minutes"
    m = re.search(
        r"(?:(\d+)\s*(?:d|day|days))?\s*"
        r"(?:(\d+)\s*(?:h|hr|hrs|hour|hours))?\s*"
        r"(?:(\d+)\s*(?:m|min|mins|minute|minutes))?", t)
    if m and any(m.groups()):
        days = int(m.group(1) or 0)
        hh = int(m.group(2) or 0)
        mm = int(m.group(3) or 0)
        return days * 86400 + hh * 3600 + mm * 60

    return None


def find_timer_text(sb) -> str:
    """多选择器兜底查找倒计时文本。"""
    candidates = [
        "span.font-mono.text-xl",
        "span.font-mono.text-lg",
        "span.text-xl",
        'span:contains("day")',
        'span:contains("days")',
        'div:contains("day")',
        'div:contains("days")',
    ]
    for selector in candidates:
        try:
            text = sb.get_text(selector)
            if text and ("day" in text.lower() or ":" in text):
                return normalize_timer_text(text)
        except Exception:
            continue

    script = r"""
    (function() {
        function normalize(s) { return s.replace(/\s+/g, ' ').trim(); }
        var els = Array.from(document.querySelectorAll('span, div'));
        for (var i = 0; i < els.length; i++) {
            var text = normalize(els[i].textContent || '');
            if (/\b\d+\s*day(s)?\b/i.test(text) || /\b\d+\s*hour(s)?\b/i.test(text))
                return text;
            if (/^\d{1,2}:\d{2}(:\d{2})?$/.test(text))
                return text;
        }
        return '';
    })()
    """
    try:
        text = sb.execute_script(script)
        if text:
            return normalize_timer_text(text)
    except Exception:
        pass
    return ""


def read_timer_seconds(sb):
    """读当前页面倒计时,返回 (秒数, 原始文本);读不到时秒数为 None。"""
    text = find_timer_text(sb)
    if not text:
        return None, ""
    return parse_timer_seconds(text), text


# ============================== 表单填充(安全转义) ==============================
def js_fill_input(sb, selector: str, text: str):
    """用 json.dumps 生成合法 JS 字面量,正确转义引号/反斜杠/换行/Unicode。"""
    selector_js = json.dumps(selector)
    value_js = json.dumps(text or "")
    sb.execute_script(f"""
    (function(){{
        var el = document.querySelector({selector_js});
        if (!el) return;
        var setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, "value"
        ).set;
        if (setter) {{ setter.call(el, {value_js}); }} else {{ el.value = {value_js}; }}
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    }})()
    """)


# ============================== 物理点击(含 HiDPI 校准) ==============================
def _activate_window():
    if os.name == "nt":
        return
    for cls in ["chrome", "chromium", "Chromium", "Chrome", "google-chrome"]:
        try:
            r = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", cls],
                               capture_output=True, text=True, timeout=3)
            wids = [w for w in r.stdout.strip().split("\n") if w.strip()]
            if wids:
                subprocess.run(["xdotool", "windowactivate", "--sync", wids[0]],
                               timeout=3, stderr=subprocess.DEVNULL)
                time.sleep(0.2)
                return
        except Exception:
            pass
    try:
        subprocess.run(["xdotool", "getactivewindow", "windowactivate"],
                       timeout=3, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _windows_mouse_click(x: int, y: int):
    try:
        user32 = ctypes.windll.user32
        # 若本进程为 DPI-aware,SetCursorPos 使用物理像素;否则 Windows 自动虚拟化。
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.08)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
        time.sleep(0.03)
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # left up
    except Exception:
        pass


def _xdotool_click(x: int, y: int):
    if os.name == "nt":
        _windows_mouse_click(x, y)
        return
    _activate_window()
    try:
        subprocess.run(["xdotool", "mousemove", "--sync", str(x), str(y)],
                       timeout=3, stderr=subprocess.DEVNULL)
        time.sleep(0.15)
        subprocess.run(["xdotool", "click", "1"], timeout=2, stderr=subprocess.DEVNULL)
    except Exception:
        os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null")


def _dpi_scale(wi) -> float:
    """确定坐标缩放系数。

    优先 DPI_SCALE 环境变量(手动强制),否则用浏览器 devicePixelRatio。
    - Linux/X11: window.screenX 是 CSS 像素,GUI 工具用物理像素,需要乘 dpr。
    - Windows DPI-aware: 同理需要乘 dpr。
    - Windows 非 DPI-aware / 缩放=100%: dpr 通常为 1,乘不乘一样。
    """
    override = os.environ.get("DPI_SCALE", "").strip()
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    try:
        return float(wi.get("dpr") or 1)
    except (TypeError, ValueError):
        return 1.0


def _click_turnstile(sb):
    try:
        coords = sb.execute_script(_COORDS_JS)
    except Exception as e:
        print(f"⚠️ 获取 Turnstile 坐标失败: {e}")
        return
    if not coords:
        print("⚠️ 无法定位 Turnstile 坐标")
        return

    try:
        wi = sb.execute_script(_WININFO_JS)
    except Exception:
        wi = {"sx": 0, "sy": 0, "oh": 800, "ih": 768, "dpr": 1}

    scale = _dpi_scale(wi)
    bar = wi["oh"] - wi["ih"]                       # 标题栏高度(CSS 像素)
    ax = int(round((coords["cx"] + wi["sx"]) * scale))
    ay = int(round((coords["cy"] + wi["sy"] + bar) * scale))
    print(f"  🖱️ 物理级点击 Turnstile ({ax}, {ay})  [dpr={wi.get('dpr')}, scale={scale}]")
    _xdotool_click(ax, ay)


def handle_turnstile(sb) -> bool:
    print("🔍 处理 Cloudflare Turnstile 验证...")
    time.sleep(2)

    if sb.execute_script(_SOLVED_JS):
        print("✅ 已静默通过")
        return True

    for _ in range(3):
        try:
            sb.execute_script(_EXPAND_JS)
        except Exception:
            pass
        time.sleep(0.5)

    for attempt in range(5):
        if sb.execute_script(_SOLVED_JS):
            print(f"✅ Turnstile 通过(第 {attempt + 1} 次尝试)")
            return True
        try:
            sb.execute_script(_EXPAND_JS)
        except Exception:
            pass
        time.sleep(0.3)

        _click_turnstile(sb)

        for _ in range(8):
            time.sleep(0.5)
            try:
                if sb.execute_script(_SOLVED_JS):
                    print(f"✅ Turnstile 通过(第 {attempt + 1} 次尝试)")
                    return True
            except Exception:
                pass
        print(f"⚠️ 第 {attempt + 1} 次未通过,重试...")

    print("❌ Turnstile 5 次验证均失败")
    return False


# ============================== 登录 ==============================
def login(sb) -> bool:
    print(f"🌐 打开登录页面: {LOGIN_URL}")
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=8)
    time.sleep(2)

    # 关闭 Cookie 弹窗
    try:
        clicked = sb.execute_script("""
            var btns = document.querySelectorAll('button');
            for (var i = 0; i < btns.length; i++) {
                if (btns[i].textContent.trim() === 'Accept All') {
                    btns[i].click(); return true;
                }
            }
            return false;
        """)
        if clicked:
            print("🍪 已关闭 Cookie 弹窗")
            time.sleep(1)
    except Exception:
        pass

    time.sleep(3)
    try:
        csrf_token = sb.get_attribute('input[name="__RequestVerificationToken"]', 'value')
        print("🔑 成功获取到验证令牌" if csrf_token
              else "⚠️ 无法获取验证令牌,尝试继续...")
    except Exception as e:
        print(f"⚠️ 获取验证令牌时出错: {e}")

    # 填邮箱 / 密码
    try:
        sb.wait_for_element("input#login", timeout=15)
        print(" 填写邮箱...")
        js_fill_input(sb, "input#login", EMAIL)
        time.sleep(1)
        sb.wait_for_element("input#password", timeout=10)
        print("🔑 填写密码...")
        js_fill_input(sb, "input#password", PASSWORD)
        time.sleep(1)
    except Exception as e:
        print(f"❌ 无法找到登录输入框: {e}")
        sb.save_screenshot("login_load_fail.png")
        return False

    # Turnstile
    if sb.execute_script(_EXISTS_JS):
        if not handle_turnstile(sb):
            return False
    else:
        print("ℹ️ 未检测到 Turnstile")

    time.sleep(1)
    print("🖱️ 点击登录按钮...")
    try:
        sb.execute_script('document.querySelector(\'button[type="submit"]\').click();')
    except Exception as e:
        print(f"⚠️ JavaScript 点击失败,尝试 CSS 选择器点击: {e}")
        try:
            sb.click('button[type="submit"]')
        except Exception:
            sb.execute_script(
                "document.querySelectorAll('button').forEach(b => "
                "{ if (b.textContent.includes('ID_SignIn')) b.click(); });")

    # 轮询等待登录结果
    print("⏳ 等待登录结果...")
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            url = (sb.get_current_url() or "").lower()
        except Exception:
            url = ""
        try:
            page = sb.get_page_source().lower()
        except Exception:
            page = ""

        if "invalid login attempt" in page:
            print("❌ 登录失败:用户名或密码错误。")
            sb.save_screenshot("login_failed.png")
            return False

        if "justrunmy.app" in url and "/account/login" not in url:
            print(f"✅ 登录成功,当前页面: {url}")
            return True

        time.sleep(1)

    print("❌ 登录超时:30 秒内未检测到跳转,请查看截图。")
    sb.save_screenshot("login_timeout.png")
    return False


# ============================== 通用按钮点击 ==============================
def click_button_by_text(sb, text: str) -> bool:
    selectors = [
        f'button[title="{text}"]',
        f'button[aria-label="{text}"]',
        f'button:has(span:contains("{text}"))',
        f'button:has(i:contains("{text}"))',
        f'button:contains("{text}")',
        f'span:contains("{text}")',
    ]
    for selector in selectors:
        try:
            sb.click(selector)
            return True
        except Exception:
            continue

    script = f"""
    (function() {{
        var expected = {json.dumps(text)};
        function normalize(s) {{ return s.replace(/\\s+/g, ' ').trim(); }}
        var els = document.querySelectorAll('button, a, [role="button"]');
        for (var i = 0; i < els.length; i++) {{
            var el = els[i];
            if (el.getAttribute('title') === expected ||
                el.getAttribute('aria-label') === expected) {{
                el.click(); return true;
            }}
            if (normalize(el.textContent).indexOf(expected) !== -1) {{
                el.click(); return true;
            }}
        }}
        return false;
    }})()
    """
    try:
        return bool(sb.execute_script(script))
    except Exception:
        return False


# ============================== 续期 ==============================
def renew(sb) -> bool:
    print("\n" + "=" * 25)
    print("   🚀 开始自动续期流程")
    print("=" * 25)

    print(f"🌐 进入控制面板: {PANEL_URL}")
    sb.open(PANEL_URL)
    time.sleep(3)

    print("🖱️ 查找应用")
    try:
        sb.wait_for_element('span:contains("Free tier")', timeout=30)
        sb.click('span:contains("Free tier")')
        time.sleep(3)
        print(f"📍 成功进入应用详情页: {sb.get_current_url()}")
    except Exception as e:
        print(f"❌ 找不到包含 'Free tier' 的应用卡片: {e}")
        sb.save_screenshot("renew_app_not_found.png")
        send_tg_message("❌", "续期失败(找不到应用)", "未知")
        return False

    app_url = sb.get_current_url()  # 记录详情页,后面刷新重读用

    # 续期前基线
    before_secs, before_text = read_timer_seconds(sb)
    print(f"⏱️ 续期前剩余时间: {before_text} ({before_secs} 秒)"
          if before_secs is not None
          else "⏱️ 续期前剩余时间: 未读取到(将退化为宽松判定)")

    print("🖱️ 点击 Reset Timer 按钮...")
    if not click_button_by_text(sb, "Reset timer"):
        print(" 找不到 Reset Timer 按钮")
        sb.save_screenshot("renew_reset_btn_not_found.png")
        send_tg_message("❌", "续期失败(找不到按钮)", "未知")
        return False
    time.sleep(3)

    print("🛡️ 检查续期弹窗内是否需要 CF 验证...")
    if sb.execute_script(_EXISTS_JS):
        if not handle_turnstile(sb):
            print(" 弹窗内的 Turnstile 验证失败")
            sb.save_screenshot("renew_turnstile_fail.png")
            send_tg_message("", "续期失败(人机验证未过)", "未知")
            return False
    else:
        print("ℹ️ 弹窗内未检测到 Turnstile")

    print("🖱️ 点击 Just Reset 确认续期...")
    if not click_button_by_text(sb, "Just Reset"):
        print(" 找不到 Just Reset 按钮")
        sb.save_screenshot("renew_just_reset_not_found.png")
        send_tg_message("❌", "续期失败(无法确认)", "未知")
        return False
    print(" 提交续期请求,等待服务器处理...")
    time.sleep(3)

    # 差值法校验
    print("🔍 验证最终倒计时状态...")
    after_secs, after_text = read_timer_seconds(sb)

    if after_secs is None:
        try:
            sb.open(app_url)
            time.sleep(3)
            after_secs, after_text = read_timer_seconds(sb)
        except Exception:
            pass

    if after_secs is None:
        print("⚠️ 无法读取续期后的倒计时,请人工核对截图。")
        sb.save_screenshot("renew_timer_read_fail.png")
        send_tg_message("⚠️", "读取剩余时间失败", "未知")
        return False

    print(f"️ 续期后剩余时间: {after_text} ({after_secs} 秒)")

    if before_secs is not None:
        delta = after_secs - before_secs
        if delta >= MIN_TIMER_INCREASE:
            print(f"✅ 续期成功:剩余时长增加 {delta // 60} 分钟。")
            send_tg_message("✅", "续期成功", after_text)
            return True
        print(f"❌ 续期失败:剩余时长未增加(变化 {delta} 秒)。")
        sb.save_screenshot("renew_failed.png")
        send_tg_message("❌", "续期失败(时长未增加)", after_text)
        return False

    print("⚠️ 缺少续期前基线,无法精确比较;已读到有效倒计时,按成功处理,建议人工复核。")
    sb.save_screenshot("renew_warning.png")
    send_tg_message("⚠️", "续期结果待人工确认", after_text)
    return True


# ============================== 主流程 ==============================
def main():
    print("=" * 25)
    print("   JustRunMy.app 自动续期")
    print("=" * 25)

    if not EMAIL or not PASSWORD:
        raise RuntimeError("缺少 JUSTRUNMY_EMAIL / JUSTRUNMY_PASSWORD 环境变量!")

    use_proxy = os.environ.get("IS_PROXY", "false").lower() == "true"
    sb_kwargs = {"uc": True, "test": True, "headless": False}
    if use_proxy:
        proxy_str = os.environ.get("PROXY_URL", "http://127.0.0.1:1081")
        print(f"🔗 挂载代理: {proxy_str}")
        sb_kwargs["proxy"] = proxy_str
    else:
        print("🌐 未使用代理,直连访问")

    # 仅屏蔽启动横幅;异常改写到日志文件而非 devnull,便于排障
    log_fp = open("sb_startup.log", "w")
    sb_cm = None
    try:
        with contextlib.redirect_stdout(log_fp), contextlib.redirect_stderr(log_fp):
            sb_cm = SB(**sb_kwargs)
            sb = sb_cm.__enter__()
    except Exception:
        log_fp.close()
        raise

    try:
        print("✅ 浏览器已启动")
        try:
            sb.open("https://api.ip.sb/ip")
            print(f"🌐 当前出口真实 IP: {sb.get_text('body').strip()}")
        except Exception:
            pass

        if login(sb):
            renew(sb)
        else:
            print("\n❌ 登录环节失败,终止后续续期操作。")
            send_tg_message("❌", "登录失败", "未知")
    finally:
        with contextlib.redirect_stdout(log_fp), contextlib.redirect_stderr(log_fp):
            if sb_cm:
                sb_cm.__exit__(None, None, None)
        log_fp.close()


if __name__ == "__main__":
    main()
