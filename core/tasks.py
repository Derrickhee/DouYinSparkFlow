import os
import re
import time
import traceback
from datetime import datetime

from playwright.sync_api import Response

from utils.logger import setup_logger
from utils.config import get_config, get_userData
from core.msg_builder import build_message, build_message_with_openai
from core.browser import get_browser


complates = {}

config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))
matchMode = config.get("matchMode", "nickname")
userIDDict = {}


def handle_response(response: Response):
    """只监听你要的那个接口响应。"""
    global userIDDict
    if "aweme/v1/creator/im/user_detail/" in response.url:
        try:
            json_data = response.json()
            for item in json_data.get("user_list", []):
                short_id = item.get("user", {}).get("ShortId")
                nickname = item.get("user", {}).get("nickname")
                user_id = item.get("user_id", "")
                if short_id:
                    userIDDict[str(short_id)] = {"nickname": nickname, "user_id": user_id}
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)
            last = tb[-1]
            print(f"解析响应失败: {e}")
            print(f"文件: {last.filename}, 行号: {last.lineno}, 函数: {last.name}")


def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    for attempt in range(retries):
        try:
            return operation(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"{name} 失败，正在重试第 {attempt + 1} 次，错误：{e}")
                time.sleep(delay)
            else:
                logger.error(f"{name} 失败，已达到最大重试次数，错误：{e}")
                raise


def safe_filename(value):
    value = re.sub(r"[^0-9A-Za-z_.-]+", "-", str(value)).strip("-")
    return value or "account"


def save_debug_artifacts(page, username, reason):
    """保存失败现场，便于在 GitHub Actions artifact 中判断页面状态。"""
    os.makedirs("logs", exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = f"logs/failure-{safe_filename(username)}-{stamp}-{safe_filename(reason)}"
    screenshot_path = f"{prefix}.png"
    html_path = f"{prefix}.html"

    try:
        page.screenshot(path=screenshot_path, full_page=True)
        logger.info(f"账号 {username} 已保存失败截图: {screenshot_path}")
    except Exception as e:
        logger.warning(f"账号 {username} 保存失败截图失败: {e}")

    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page.content())
        logger.info(f"账号 {username} 已保存失败页面 HTML: {html_path}")
    except Exception as e:
        logger.warning(f"账号 {username} 保存失败页面 HTML 失败: {e}")


def log_page_snapshot(page, username, reason):
    try:
        body_text = page.locator("body").inner_text(timeout=2000)
        compact_text = " ".join(body_text.split())[:500]
    except Exception as e:
        compact_text = f"读取页面文本失败: {e}"
    logger.info(f"账号 {username} 页面快照({reason}) URL={page.url} title={page.title()} body={compact_text}")


def normalize_targets(targets):
    return {str(target).strip() for target in targets if str(target).strip()}


def first_visible_locator(page, selectors, timeout=3000):
    last_error = None
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout)
            return locator, selector
        except Exception as e:
            last_error = e
    raise Exception(f"未找到可见元素，候选选择器: {selectors}，最后错误: {last_error}")


def click_first_visible(page, selectors, description, username, timeout=3000):
    locator, selector = first_visible_locator(page, selectors, timeout=timeout)
    locator.click()
    logger.info(f"账号 {username} {description}成功，使用选择器: {selector}")
    return locator


def first_attached_selector(page, selectors, timeout=1500):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="attached", timeout=timeout)
            return selector
        except Exception:
            continue
    return None


def find_scrollable_friend_container(page):
    selectors = [
        "css=#sub-app .semi-list-items",
        "css=#sub-app [class*='semi-list'] ul",
        "css=#sub-app [class*='conversation']",
        "css=#sub-app [class*='chat'] ul",
        'xpath=//*[@id="sub-app"]//ul/ancestor::div[.//div[contains(@class, "semi-list-item-body")]][1]',
        'xpath=//*[@id="sub-app"]//div[.//ul][@style or contains(@class, "scroll")][1]',
        'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]/div/div/div[3]/div/div/div/ul/div',
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="attached", timeout=1500)
            handle = locator.element_handle()
            if handle:
                return handle, selector
        except Exception:
            continue

    handle = page.evaluate_handle(
        """
        () => {
            const root = document.querySelector('#sub-app') || document.body;
            const nodes = Array.from(root.querySelectorAll('div, ul'));
            return nodes.find((node) => node.scrollHeight > node.clientHeight + 20) || null;
        }
        """
    )
    element = handle.as_element()
    if element:
        return element, "auto-scrollable"
    return None, None


def target_symbol_for_friend(friend_name):
    if matchMode == "short_id":
        return next(
            (sid for sid, info in userIDDict.items() if info.get("nickname") == friend_name),
            None,
        )
    return friend_name


def scroll_and_select_user(page, username, targets):
    """滚动并选择目标好友；目标没找到时抛错，避免 workflow 静默成功。"""
    targets = normalize_targets(targets)
    if not targets:
        raise Exception(f"账号 {username} 未配置 targets，停止执行")

    friends_tab_selectors = [
        'xpath=//*[@id="sub-app"]//*[normalize-space()="好友"]',
        'xpath=//*[@id="sub-app"]//*[contains(normalize-space(), "好友") and (self::div or self::span or self::button)]',
        'xpath=//*[contains(@class, "semi-tabs-tab") and .//*[contains(normalize-space(), "好友")]]',
        'xpath=//*[contains(@class, "semi-tabs-tab") and contains(normalize-space(), "好友")]',
        'text=/^好友$/',
        'xpath=//*[@id="sub-app"]/div/div/div[1]/div[2]',
    ]
    target_selectors = [
        "css=#sub-app .semi-list-item-body.semi-list-item-body-flex-start",
        "css=#sub-app .semi-list-item-body",
        "css=#sub-app li[class*='semi-list-item']",
        "css=#sub-app [class*='conversation'] [class*='item']",
        "css=#sub-app [class*='chat'] li",
        'xpath=//*[@id="sub-app"]//div[contains(@class, "semi-list-item-body") and contains(@class, "semi-list-item-body-flex-start")]',
        'xpath=//*[@id="sub-app"]//li[.//span[normalize-space()]]',
        'xpath=//*[@id="sub-app"]/div/div[1]/div[2]/div[2]//div[contains(@class, "semi-list-item-body semi-list-item-body-flex-start")]',
    ]
    first_friend_selectors = [
        "css=#sub-app .semi-list-item:first-child",
        "css=#sub-app li[class*='semi-list-item']:first-child",
        "css=#sub-app [class*='conversation'] [class*='item']:first-child",
        'xpath=//*[@id="sub-app"]//li[contains(@class, "semi-list-item")][1]',
        'xpath=//*[@id="sub-app"]//li[.//span[normalize-space()]][1]',
        'xpath=//*[@id="sub-app"]/div/div/div[2]/div[2]/div/div/div[1]/div/div/div/ul/div/div/div[1]/li/div',
    ]
    no_more_selector = 'xpath=//*[contains(@class, "no-more-tip-") or contains(normalize-space(), "没有更多") or contains(normalize-space(), "到底") or contains(normalize-space(), "暂无更多")]'
    loading_selector = 'xpath=//*[contains(@class, "semi-spin") or contains(normalize-space(), "加载中")]'

    logger.info(f"账号 {username} 当前目标列表: {sorted(targets)}，匹配模式: {matchMode}")
    logger.info(f"账号 {username} 准备点击好友标签页")

    try:
        click_first_visible(page, friends_tab_selectors, "点击好友标签页", username, timeout=5000)
    except Exception as e:
        log_page_snapshot(page, username, "friends-tab-not-found")
        list_selector = first_attached_selector(page, first_friend_selectors + target_selectors, timeout=3000)
        if list_selector:
            logger.warning(
                f"账号 {username} 未找到好友标签页，但检测到好友/聊天列表元素，"
                f"将跳过标签页点击继续处理。列表选择器: {list_selector}，错误: {e}"
            )
        else:
            save_debug_artifacts(page, username, "friends-tab-not-found")
            raise Exception(f"账号 {username} 找不到好友标签页，也未检测到好友/聊天列表，页面结构可能变化或未进入消息页")

    try:
        first_friend, first_friend_selector = first_visible_locator(page, first_friend_selectors, timeout=config["browserTimeout"])
        first_friend.click()
        logger.info(f"账号 {username} 已激活好友列表，使用选择器: {first_friend_selector}")
    except Exception:
        save_debug_artifacts(page, username, "friend-list-not-loaded")
        raise Exception(f"账号 {username} 好友列表未加载，可能是登录页、验证页或页面结构变化")

    time.sleep(config["friendListTimeout"] / 1000)

    found_friends = set()
    selected_targets = set()
    remaining_targets = set(targets)
    empty_scroll_count = 0
    max_empty_scrolls = 10

    while True:
        target_elements = []
        for selector in target_selectors:
            elements = page.locator(selector).all()
            if elements:
                target_elements = elements
                logger.debug(f"账号 {username} 使用好友条目选择器: {selector}")
                break

        if not target_elements:
            save_debug_artifacts(page, username, "friend-items-not-found")
            raise Exception(f"账号 {username} 未找到好友条目元素，页面结构可能变化")

        prev_found_count = len(found_friends)
        matched_this_round = False

        for element in target_elements:
            try:
                name_locator = element.locator(
                    'xpath=.//span[contains(@class, "item-header-name-") or contains(@class, "name")][1]'
                )
                if name_locator.count() == 0:
                    name_locator = element.locator("xpath=.//span[normalize-space()][1]")

                friend_name = name_locator.first.inner_text(timeout=2000).strip()
                if not friend_name or friend_name in found_friends:
                    continue

                found_friends.add(friend_name)
                target_symbol = target_symbol_for_friend(friend_name)
                logger.info(
                    f"账号 {username} 找到好友: {friend_name}"
                    + (f"，ShortId: {target_symbol}" if target_symbol else "")
                )

                if target_symbol in remaining_targets:
                    element.click()
                    selected_targets.add(target_symbol)
                    remaining_targets.remove(target_symbol)
                    matched_this_round = True
                    logger.info(f"账号 {username} 选中目标好友 {friend_name}，目标标识: {target_symbol}")
                    yield {"friend_name": friend_name, "target": target_symbol}

                    if not remaining_targets:
                        logger.info(f"账号 {username} 所有目标好友均已找到: {sorted(selected_targets)}")
                        return
                    break
            except Exception as e:
                logger.debug(f"账号 {username} 解析好友条目失败: {e}")

        if matched_this_round:
            continue

        if len(found_friends) > prev_found_count:
            empty_scroll_count = 0
        else:
            empty_scroll_count += 1

        if page.locator(no_more_selector).count() > 0:
            logger.info(f"账号 {username} 检测到好友列表已到底")
            break

        if empty_scroll_count >= max_empty_scrolls:
            logger.warning(f"账号 {username} 连续 {max_empty_scrolls} 次滚动未发现新好友，判定已到达底部")
            break

        if page.locator(loading_selector).count() > 0:
            logger.info(f"账号 {username} 好友列表正在加载中")
            time.sleep(1.5)

        scrollable_element, scroll_selector = find_scrollable_friend_container(page)
        if not scrollable_element:
            save_debug_artifacts(page, username, "friend-scroll-container-not-found")
            raise Exception(f"账号 {username} 未找到好友列表滚动容器")

        scroll_top_before = page.evaluate("(element) => element.scrollTop", scrollable_element)
        page.evaluate("(element) => element.scrollTop += 800", scrollable_element)
        time.sleep(0.3)
        scroll_top_after = page.evaluate("(element) => element.scrollTop", scrollable_element)

        if scroll_top_before == scroll_top_after:
            empty_scroll_count += 2
            logger.debug(
                f"账号 {username} 好友列表滚动位置未变化 ({scroll_top_before})，"
                f"滚动容器: {scroll_selector}，空滚动计数: {empty_scroll_count}/{max_empty_scrolls}"
            )
        else:
            logger.debug(
                f"账号 {username} 滚动好友列表加载更多好友，滚动容器: {scroll_selector}，"
                f"scrollTop: {scroll_top_before} -> {scroll_top_after}"
            )
        time.sleep(1.5)

    logger.info(f"账号 {username} 本次扫描找到好友: {sorted(found_friends)}")
    logger.info(f"账号 {username} 已找到目标: {sorted(selected_targets)}")
    logger.error(f"账号 {username} 未找到目标: {sorted(remaining_targets)}")
    save_debug_artifacts(page, username, "missing-targets")
    raise Exception(f"账号 {username} 未找到目标好友: {sorted(remaining_targets)}")


def get_chat_input(page, username):
    selectors = [
        "css=[class*='chat-input-']",
        "css=[contenteditable='true']",
        "css=textarea",
        'xpath=//div[contains(@class, "chat-input-") or @contenteditable="true"]',
    ]
    try:
        locator, selector = first_visible_locator(page, selectors, timeout=config["browserTimeout"])
        logger.info(f"账号 {username} 已找到聊天输入框，使用选择器: {selector}")
        return locator
    except Exception:
        save_debug_artifacts(page, username, "chat-input-not-found")
        raise Exception(f"账号 {username} 未找到聊天输入框")


def click_send_button_or_press_enter(page, chat_input, username, friend_name):
    send_selectors = [
        'xpath=//button[not(@disabled) and contains(normalize-space(), "发送")]',
        'xpath=//*[contains(@class, "semi-button") and not(@disabled) and contains(normalize-space(), "发送")]',
        'xpath=//*[@role="button" and contains(normalize-space(), "发送")]',
        'text=/^发送$/',
    ]

    for selector in send_selectors:
        try:
            button = page.locator(selector).first
            button.wait_for(state="visible", timeout=1500)
            if button.is_enabled():
                button.click()
                logger.info(f"账号 {username} 已点击发送按钮给好友 {friend_name}，使用选择器: {selector}")
                return "click"
        except Exception:
            continue

    chat_input.press("Enter")
    logger.info(f"账号 {username} 未找到可点击发送按钮，已按 Enter 发送给好友 {friend_name}")
    return "enter"


def message_occurrence_count(page, message):
    return page.evaluate(
        """
        (message) => {
            const text = document.body ? document.body.innerText : '';
            if (!message) return 0;
            return text.split(message).length - 1;
        }
        """,
        message,
    )


def input_contains_message(chat_input, message):
    try:
        return message in chat_input.inner_text(timeout=500)
    except Exception:
        try:
            return message in chat_input.input_value(timeout=500)
        except Exception:
            return False


def verify_message_sent(page, chat_input, message, previous_count, username, friend_name):
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            count = message_occurrence_count(page, message)
            if count > previous_count and not input_contains_message(chat_input, message):
                logger.info(f"账号 {username} 发送后已检测到新消息出现在聊天记录中，好友: {friend_name}")
                return True
        except Exception:
            pass
        time.sleep(0.5)

    logger.error(f"账号 {username} 发送后未检测到消息气泡或发送成功状态，好友: {friend_name}")
    save_debug_artifacts(page, username, f"send-not-verified-{friend_name}")
    return False


def do_user_task(browser, username, cookies, targets):
    context = browser.new_context()
    context.set_default_navigation_timeout(config["browserTimeout"])
    context.set_default_timeout(config["browserTimeout"])

    page = context.new_page()

    try:
        if matchMode == "short_id":
            page.on("response", handle_response)

        retry_operation(
            "打开抖音创作者中心",
            page.goto,
            retries=config["taskRetryTimes"],
            delay=5,
            url="https://creator.douyin.com/",
        )
        context.add_cookies(cookies)

        retry_operation(
            "导航到消息页面",
            page.goto,
            retries=config["taskRetryTimes"],
            delay=5,
            url="https://creator.douyin.com/creator-micro/data/following/chat",
        )
        logger.info(f"账号 {username} 进入消息页成功: {page.url}")
        logger.info(f"账号 {username} 开始发送消息，目标列表: {sorted(normalize_targets(targets))}")

        sent_targets = []
        failed_targets = []

        for selected in scroll_and_select_user(page, username, targets):
            friend_name = selected["friend_name"]
            target = selected["target"]
            logger.info(f"账号 {username} 准备给好友 {friend_name} 输入消息，目标标识: {target}")

            chat_input = get_chat_input(page, username)
            message = build_message()
            previous_message_count = message_occurrence_count(page, message)
            lines = message.split("\n")
            for index, line in enumerate(lines):
                chat_input.type(line)
                if index < len(lines) - 1:
                    chat_input.press("Shift+Enter")

            logger.info(f"账号 {username} 已给好友 {friend_name} 输入消息，准备发送")
            send_method = click_send_button_or_press_enter(page, chat_input, username, friend_name)
            logger.info(f"账号 {username} 发送动作已执行，方式: {send_method}，好友: {friend_name}")

            if verify_message_sent(page, chat_input, message, previous_message_count, username, friend_name):
                sent_targets.append(target)
                logger.info(f"账号 {username} 给好友 {friend_name} 发送完成，目标标识: {target}")
            else:
                failed_targets.append(target)

            time.sleep(2)

        missing_after_send = normalize_targets(targets) - set(sent_targets)
        if failed_targets or missing_after_send:
            logger.error(
                f"账号 {username} 任务未完成，发送失败或未确认目标: "
                f"failed={sorted(set(failed_targets))}, missing_or_unsent={sorted(missing_after_send)}"
            )
            raise Exception(
                f"账号 {username} 发送失败或未确认: "
                f"failed={sorted(set(failed_targets))}, missing_or_unsent={sorted(missing_after_send)}"
            )

        logger.info(f"账号 {username} 所有目标发送完成并通过验证: {sorted(sent_targets)}")
    except Exception:
        save_debug_artifacts(page, username, "task-error")
        raise
    finally:
        context.close()


def runTasks():
    playwright, browser = get_browser()
    try:
        logger.info("开始执行任务")
        logger.debug(f"当前配置如下：")
        logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')}")
        logger.debug(f"一言类型: {config['hitokotoTypes']}")
        for user in userData:
            logger.info(f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}")

        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            complates[user["unique_id"]] = []
            username = user.get("username", "未知用户")
            logger.info(f"开始处理账号 {username}")
            do_user_task(browser, username, cookies, targets)
            logger.info(f"账号 {username} 任务完成")
    finally:
        browser.close()
        playwright.stop()
