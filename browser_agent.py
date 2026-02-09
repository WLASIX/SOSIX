"""
Main browser agent module.
Coordinates all components to execute user tasks autonomously.
"""
from typing import Optional, Dict, Any
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from nvidia_api import NvidiaAPIClient
from config_loader import config
from page_analyzer import PageAnalyzer, PageAnalysis
from action_executor import ActionExecutor
from task_analyzer import TaskAnalyzer, Task
from decision_validator import DecisionValidator
from logger import logger
import asyncio
import json
import re
import hashlib


class BrowserAgent:
    """Autonomous browser agent for task execution"""

    def __init__(self):
        # Load configuration
        self.nvidia_config = config.get_nvidia_api_config()
        self.browser_config = config.get_browser_config()
        self.agent_config = config.get_agent_config()
        
        # Initialize API client
        self.api = NvidiaAPIClient(self.nvidia_config)
        
        # Initialize components
        self.task_analyzer = TaskAnalyzer(self.api)
        
        # Playwright components
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.executor: Optional[ActionExecutor] = None
        self.analyzer: Optional[PageAnalyzer] = None
        
        # Task state
        self.current_task: Optional[Task] = None
        self.iteration_count = 0
        self.max_iterations = self.agent_config.get("max_iterations", 50)
        
        # Page state tracking - detect if page didn't change after action
        self.previous_page_state: Optional[str] = None  # Fingerprint of previous page
        self.page_state_unchanged_count = 0  # How many actions in a row didn't change page
        self.max_unchanged_threshold = 2  # Max consecutive unchanged states before error
        
        # Circuit breaker for stuck detection
        self.error_history = []  # List of last N errors
        self.last_error = None
        self.consecutive_error_threshold = 3  # Stop if same error 3 times
        self.max_error_history = 5  # Keep last 5 errors
        
        # Memory of failed actions during THIS task execution
        self.failed_actions = []  # List of {"element": elem_id, "action": action, "reason": reason}

    async def initialize(self):
        """Инициализация браузера и компонентов"""
        logger.start()
        logger.info("🚀 Инициализация агента")
        
        try:
            # Start playwright
            logger.info("Запускаю Playwright...")
            playwright = await async_playwright().start()
            
            # Launch browser
            logger.info("Запускаю браузер...")
            self.browser = await playwright.chromium.launch(
                headless=self.browser_config.get("headless", False),
                slow_mo=self.browser_config.get("slow_motion", 0)
            )
            
            # Create context for persistent session
            logger.info("Создаю контекст браузера...")
            self.context = await self.browser.new_context(
                viewport=self.browser_config.get("viewport")
            )
            
            # Create page
            logger.info("Создаю страницу...")
            self.page = await self.context.new_page()
            
            # Initialize helpers
            self.executor = ActionExecutor(self.page)
            self.analyzer = PageAnalyzer(self.page)
            
            # Set up API system prompt
            self.api.set_system_message(self.task_analyzer.get_system_prompt())
            
            logger.success("Агент успешно инициализирован")
            
        except Exception as e:
            logger.error(f"Ошибка инициализации: {str(e)}")
            raise

    async def shutdown(self):
        """Остановка браузера и очистка"""
        logger.info("Останавливаю агент...")
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            logger.success("Агент остановлен")
        except Exception as e:
            logger.error(f"Ошибка при остановке: {str(e)}")

    async def execute_task(self, task_description: str) -> Dict[str, Any]:
        """
        Выполнить задачу: анализ → открыть страницу → цикл действий
        
        Args:
            task_description: Описание задачи на естественном языке
            
        Returns:
            Результат выполнения задачи
        """
        logger.start()
        logger.info("📋 Анализирую задачу...")
        
        try:
            # Quick task analysis (goal + type + risk only)
            self.current_task = await self.task_analyzer.analyze_task(task_description)
            
            # ========== DETERMINE STARTING URL (NO HARDCODED MAPPING) ==========
            # Pass FULL task description to LLM, not shortened goal
            start_url = await self._get_start_url_from_task(task_description)
            if not start_url:
                logger.error("❌ Не удалось определить стартовый URL")
                return {
                    "status": "ошибка",
                    "error": "Не удалось определить стартовый URL. Пожалуйста, проверьте ввод."
                }
            
            # ========== HANDLE BROWSER COMMANDS ==========
            # If user issued a browser command (like "go back"), execute it and continue
            if start_url == "BROWSER_BACK":
                logger.action("⬅️  Выполняю: go back")
                await self.page.go_back()
                start_url = self.page.url
                logger.success(f"✅ Текущая страница: {start_url}")
            elif start_url == "BROWSER_FORWARD":
                logger.action("➡️  Выполняю: go forward")
                await self.page.go_forward()
                start_url = self.page.url
                logger.success(f"✅ Текущая страница: {start_url}")
            elif start_url == "BROWSER_REFRESH":
                logger.action("🔄 Выполняю: refresh page")
                await self.page.reload()
                start_url = self.page.url
                logger.success(f"✅ Страница обновлена")
            
            # Go directly to the page
            logger.info(f"🌐 Открываю: {start_url}")
            
            page_load_success = False
            current_url = start_url
            retry_count = 0
            MAX_RETRIES = 1
            
            while retry_count <= MAX_RETRIES and not page_load_success:
                try:
                    # Use domcontentloaded instead of networkidle for heavy JS sites (Avito, Yandex, etc)
                    # This waits for page structure but not all async scripts to complete
                    await self.page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
                    logger.success("✅ Страница загружена")
                    page_load_success = True
                    
                except Exception as e:
                    error_str = str(e).lower()
                    logger.error(f"❌ Ошибка при открытии страницы: {str(e)[:100]}")
                    
                    # Check if it's a network error (page not found / domain not exists)
                    if "err_name_not_resolved" in error_str or "err_address_unreachable" in error_str:
                        retry_count += 1
                        
                        if retry_count <= MAX_RETRIES:
                            # СТРАТЕГИЯ: Если сайт не найден → спросить модель искать через Google
                            logger.warning(f"⚠️ Страница недоступна: {current_url}")
                            logger.warning(f"🔍 Ищу альтернативный способ через Google...")
                            
                            try:
                                # Ask LLM to find the page via Google search
                                find_page_prompt = f"""
Задача: {task_description}

Предложенный URL недоступен: {current_url}

НЕОБХОДИМО:
1. Определить что нужно искать в Google (поисковый запрос)
2. Вернуть поисковый запрос на русском языке

Ответ ТОЛЬКО JSON (без ```):
{{
  "search_query": "точный поисковый запрос без кавычек"
}}
"""
                                search_response = await self.api.call_async(find_page_prompt, use_history=False)
                                
                                if not search_response:
                                    logger.debug(f"  ⚠️ Пустой ответ на поиск в search_hints")
                                    return None
                                    
                                try:
                                    search_json = json.loads(search_response.strip())
                                    search_query = search_json.get("search_query", "").strip()
                                    
                                    if search_query:
                                        # Build Google search URL
                                        current_url = f"https://www.google.com/search?q={search_query.replace(' ', '+')}"
                                        logger.success(f"✅ Ищу через Google: '{search_query}'")
                                        logger.info(f"Перехожу на: {current_url}")
                                        continue  # Retry with Google search URL
                                    else:
                                        logger.error("❌ Модель не вернула поисковый запрос")
                                        return {
                                            "status": "ошибка_навигации",
                                            "message": f"Страница недоступна и не удалось найти альтернативу: {current_url}",
                                            "error": str(e)
                                        }
                                except json.JSONDecodeError:
                                    logger.error("❌ Модель вернула невалидный JSON")
                                    return {
                                        "status": "ошибка_навигации",
                                        "message": f"Ошибка при получении поискового запроса",
                                        "error": search_response[:100]
                                    }
                                    
                            except Exception as search_error:
                                logger.error(f"❌ Ошибка при поиске альтернативы: {str(search_error)[:80]}")
                                return {
                                    "status": "ошибка_сети",
                                    "message": f"Страница недоступна: {current_url}",
                                    "error": str(e)
                                }
                        else:
                            # Max retries exceeded
                            return {
                                "status": "ошибка_сети",
                                "message": f"Страница недоступна даже через Google: {current_url}",
                                "error": str(e)
                            }
                    else:
                        # Other error (timeout, browser error, etc)
                        return {
                            "status": "ошибка_навигации",
                            "message": "Ошибка при открытии страницы",
                            "error": str(e)
                        }
            
            # ========== VERIFY PAGE IS CORRECT ==========
            # If we loaded a Google search page, check if main result looks relevant
            if "google.com/search" in current_url:
                logger.info("📝 Проверяю результаты поиска...")
                
                # Analyze page to see if we found relevant results
                page_text = await self.page.evaluate("() => document.body.innerText")
                
                is_relevant = await self._check_if_search_results_relevant(page_text, task_description)
                
                if not is_relevant:
                    logger.warning("⚠️ Результаты поиска не выглядят релевантными")
                    logger.warning("⚠️ Попробую уточнить поисковый запрос...")
                    
                    # Ask model to refine search
                    refine_prompt = f"""
Исходная задача: {task_description}

Поисковый запрос: {current_url.split('q=')[1] if 'q=' in current_url else 'unknown'}

Результаты поиска показывают нерелевантную информацию.

НЕОБХОДИМО:
Вернуть уточненный поисковый запрос.

Ответ ТОЛЬКО JSON (без ```):
{{
  "search_query": "уточненный поисковый запрос"
}}
"""
                    try:
                        refine_response = await self.api.call_async(refine_prompt, use_history=False)
                        if refine_response:
                            refined_json = json.loads(refine_response.strip())
                            refined_query = refined_json.get("search_query", "").strip()
                            
                            if refined_query:
                                current_url = f"https://www.google.com/search?q={refined_query.replace(' ', '+')}"
                                logger.info(f"📝 Уточняю поиск: {refined_query}")
                                await self.page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
                                logger.success("✅ Новые результаты поиска загружены")
                        else:
                            logger.debug(f"  ⚠️ Пустой ответ при уточнении")
                    except Exception as refine_error:
                        logger.warning(f"⚠️ Ошибка при уточнении поиска: {str(refine_error)[:60]}")
                        # Continue anyway with current results
            
            # Execute task iteratively (scan → decide → act loop)
            self.iteration_count = 0
            result = await self._execute_iteratively()
            
            return result
            
        except KeyboardInterrupt:
            logger.warning("Выполнение задачи прервано пользователем (Ctrl+C)")
            return {
                "status": "прервано",
                "message": "Задача прервана пользователем"
            }
        except Exception as e:
            logger.error(f"Ошибка выполнения задачи: {str(e)}")
            return {
                "status": "ошибка",
                "error": str(e)
            }

    async def _get_start_url_from_task(self, task_description: str) -> Optional[str]:
        """
        Ask LLM which URL is needed to solve the task.
        No hardcoding - model decides based on FULL task context.
        
        Args:
            task_description: Full original task description (not shortened goal!)
        
        Strategy:
        1. LLM analyzes full task and decides if it needs:
           - Specific site (dodo.pizza, example.com, etc)
           - Google search for the query
           - Other approach
        2. User input only if LLM doesn't know the site
        
        Returns:
            URL to navigate to, or None if user cancelled
        """
        logger.analysis("🌐 Определение стартовой страницы")
        
        # ========== CHECK FOR EXPLICIT URL IN ORIGINAL TASK ==========
        url_pattern = r'https?://[^\s]+'
        urls = re.findall(url_pattern, task_description)
        if urls:
            found_url = urls[0]
            logger.success(f"✅ URL найден в задаче: {found_url}")
            return found_url
        
        # ========== ASK LLM WHICH URL TO USE (WITH FULL CONTEXT!) ==========
        logger.info("📞 Спрашиваю модель: какой URL подойдёт?")
        
        llm_prompt = f"""
ЗАДАЧА: Определить стартовый URL (ТОЛЬКО базовая страница сайта!) для выполнения пользовательской задачи.

Задача пользователя: {task_description}

═══════════════════════════════════════════════════════════════

ИСПОЛЬЗУЙ СВОИ ЗНАНИЯ ДЛЯ РЕШЕНИЯ:

- Если пользователь упомянул конкретный сайт → используй его базовый URL (БЕЗ query параметров!)
- Для поисковых сайтов (YouTube, Google и т.д.): возвращай ТОЛЬКО базовый URL!
  ✗ НЕПРАВИЛЬНО: https://www.youtube.com/results?search_query=музыка
  ✓ ПРАВИЛЬНО: https://www.youtube.com
  Поиск будет выполнен через заполнение формы поиска на странице!
- Если не упомянул сайт → решай на основе типа задачи
- Если неуверен → используй Google Search

АЛГОРИТМ:
─────────
1. Есть ли явное упоминание сайта? → базовый URL только (без query)!
2. Можно ли определить тип задачи? → подходящий сайт (базовый URL!)
3. Неясно? → используй Google Search

═══════════════════════════════════════════════════════════════

Ответьте JSON в формате (ТОЛЬКО ОДИН объект, БЕЗ МАССИВА):
{{
  "url_type": "specific_site" | "search",
  "url": "https://..." если specific_site, ТОЛЬКО БАЗОВЫЙ URL (БЕЗ QUERY!), иначе пустая строка,
  "search_query": "поисковый запрос" если search, иначе пустая строка,
  "reason": "одна строка - почему выбран этот способ"
}}

═══════════════════════════════════════════════════════════════

ПРИМЕРЫ (модель решает сама!):

Вход: "открой e-commerce сайт и найди товар"
→ specific_site, потому что явно упомянут тип сайта
→ url: "https://aliexpress.com" (базовый URL, не с фильтром!)

Вход: "найди видео на youtube"
→ specific_site, потому что явно YouTube
→ url: "https://www.youtube.com" (ТОЛЬКО базовая страница!)

Вход: "найди видео в интернете"
→ search (не упомянут conкретный сайт)
→ search_query: "видео" или что-то релевантное

Вход: "где купить товар"
→ search, потому что можно найти много вариантов

═══════════════════════════════════════════════════════════════

⚠️ КРИТИЧНО:
- Для поисковых сайтов: ТОЛЬКО базовый URL (без /results, без ?search_query, без фильтров!)
- Поиск будет выполнен ЧЕРЕЗ заполнение формы поиска на самой странице!
- Всегда один JSON объект (не массив!)
- Полные URL с https://
"""
        
        try:
            llm_response = await self.api.decide_async(llm_prompt)
            logger.debug(f"📥 Полный ответ модели:\n{llm_response}")
            
            # ========== PARSE JSON FROM LLM RESPONSE ==========
            import json
            response_json = None
            
            try:
                # Try direct parse first
                response_json = json.loads(llm_response.strip())
            except json.JSONDecodeError:
                # Try to extract JSON from response (in case there's extra text)
                json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', llm_response, re.DOTALL)
                if json_match:
                    try:
                        response_json = json.loads(json_match.group())
                    except json.JSONDecodeError:
                        logger.error("❌ Не удалось распарсить JSON из ответа модели")
                        response_json = None
                else:
                    logger.error("❌ Не найден JSON в ответе модели")
                    response_json = None
            
            if not response_json or not isinstance(response_json, dict):
                logger.error(f"❌ Ответ модели не является JSON объектом: {llm_response[:100]}")
                return None
            
            # ========== EXTRACT FIELDS FROM RESPONSE ==========
            url_type = response_json.get("url_type", "").lower().strip()
            url = response_json.get("url", "").strip()
            search_query = response_json.get("search_query", "").strip()
            reason = response_json.get("reason", "")
            
            logger.info(f"📊 Модель определила: {url_type}")
            if reason:
                logger.debug(f"   └─ Причина: {reason}")
            
            # ========== PROCESS RESPONSE ==========
            
            # Case 1: Specific site URL
            if url_type == "specific_site":
                if not url:
                    logger.error("❌ Модель выбрала specific_site но URL пуст")
                    return None
                
                # Validate URL format
                if url.startswith("http://") or url.startswith("https://"):
                    logger.success(f"✅ Стартовый URL: {url}")
                    return url
                elif "." in url:
                    # Add https:// prefix
                    full_url = f"https://{url}"
                    logger.success(f"✅ Стартовый URL: {full_url}")
                    return full_url
                else:
                    logger.error(f"❌ URL не валиден: {url}")
                    return None
            
            # Case 2: Google search
            elif url_type == "search":
                if not search_query:
                    logger.error("❌ Модель выбрала search но query пуст")
                    return None
                
                search_url = f"https://www.google.com/search?q={search_query.replace(' ', '+')}"
                logger.success(f"✅ Google поиск: '{search_query}'")
                return search_url
            
            # Unexpected value
            else:
                logger.error(f"❌ Неожиданный url_type: '{url_type}'")
                logger.error(f"   Полный ответ: {response_json}")
                return None
            
        except Exception as e:
            logger.error(f"❌ Ошибка при определении URL: {str(e)}")
            # Ask user as last resort with smart interpretation
            user_input = logger.ask_user("Что вам нужно найти или на какой сайт перейти? (или команда: 'назад', 'вперед', и т.д.)")
            if not user_input:
                return None
            
            # Use LLM to interpret user's command intelligently
            interpreted_url = await self._interpret_user_command(user_input)
            return interpreted_url

    async def _interpret_user_command(self, user_input: str) -> Optional[str]:
        """
        🧠 Умная интерпретация команд пользователя.
        Модель понимает что пользователь имел в виду и преобразует в действие.
        
        Поддерживаемые команды:
        - Команды браузера: "назад", "вернись назад", "обнови" и т.д.
        - Поиск: "найди <что-то>" или "поиск <что-то>"
        - Сайты: пользователь упоминает конкретный сайт
        
        Args:
            user_input: Ввод пользователя на естественном языке
            
        Returns:
            URL для навигации, либо специальный маркер для браузер команды
        """
        logger.info(f"🧠 Умная интерпретация команды: '{user_input}'")
        
        interpret_prompt = f"""
ЗАДАЧА: Интерпретировать команду пользователя и определить действие.

Команда пользователя: {user_input}

═══════════════════════════════════════════════════════════════

ТИПЫ КОМАНД:

1. БРАУЗЕР КОМАНДЫ:
   - Вернуться назад
   - Перейти вперед  
   - Перезагрузить страницу
   - Остановить/выход

2. НАВИГАЦИЯ НА САЙТ:
   - Упомянут конкретный сайт → открыть его

3. ПОИСК:
   - Нужно найти что-то → поисковый запрос

═══════════════════════════════════════════════════════════════

Используй свой интеллект для распознавания:
- Сайты: определи по типу контента (e-commerce, видео, поиск и т.д.)
- Команды: "назад", "стоп", "обнови" и т.д. имеют понятное значение
- Поиск: преобразуй команду в поисковый запрос если нужен поиск

ОТВЕТ ФОРМАТ (JSON объект - БЕЗ МАССИВА!):

Для браузер команд:
{{
  "type": "browser_action",
  "action": "back" | "forward" | "refresh" | "cancel",
  "reason": "объяснение"
}}

Для навигации на сайт:
{{
  "type": "url",
  "url": "https://...",
  "reason": "объяснение"
}}

Для поиска:
{{
  "type": "search",
  "query": "поисковый запрос",
  "reason": "объяснение"
}}

═══════════════════════════════════════════════════════════════

ПРИМЕРЫ:

1. "вернись назад"
   → browser_action, действие: back

2. "открой видео сайт"
   → url для видео платформы (модель знает какой)

3. "найди цену товара"
   → search, запрос: "цена товара"

═══════════════════════════════════════════════════════════════

ВАЖНО:
- Всегда один JSON объект (не массив!)
- Используй встроенное знание модели о сайтах
- Полные URL для навигации (https://...)
- Для поиска — точный запрос на основе команды
"""
        
        try:
            logger.debug(f"  📞 Отправляю команду на анализ LLM...")
            response = await self.api.call_async(interpret_prompt, use_history=False)
            if not response:
                logger.error("❌ API вернул пустой ответ")
                return None
            logger.debug(f"  📥 Ответ LLM: {response[:200]}")
            
            # ========== PARSE JSON ==========
            import json
            response_json = None
            
            try:
                response_json = json.loads(response.strip())
            except json.JSONDecodeError:
                # Try to extract JSON from response
                json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', response, re.DOTALL)
                if json_match:
                    try:
                        response_json = json.loads(json_match.group())
                    except json.JSONDecodeError:
                        logger.error("❌ Не удалось распарсить JSON")
                        response_json = None
            
            if not response_json or not isinstance(response_json, dict):
                logger.error(f"❌ Некорректный ответ LLM: {response[:100]}")
                return None
            
            # ========== PROCESS RESPONSE ==========
            response_type = response_json.get("type", "").lower().strip()
            
            # Type 1: Browser action
            if response_type == "browser_action":
                action = response_json.get("action", "").lower().strip()
                reason = response_json.get("reason", "")
                
                logger.info(f"🔄 Браузер команда: {action}")
                
                # Return special marker for browser actions
                # These will be handled in execute_task method
                if action == "back":
                    logger.success(f"⬅️  Вернусь назад")
                    return "BROWSER_BACK"
                elif action == "forward":
                    logger.success(f"➡️  Пойду вперед")
                    return "BROWSER_FORWARD"
                elif action == "refresh":
                    logger.success(f"🔄 Обновлю страницу")
                    return "BROWSER_REFRESH"
                elif action == "cancel":
                    logger.warning(f"⛔ Пользователь отменил операцию")
                    return None
                else:
                    logger.error(f"❌ Неизвестная браузер команда: {action}")
                    return None
            
            # Type 2: URL navigation
            elif response_type == "url":
                url = response_json.get("url", "").strip()
                reason = response_json.get("reason", "")
                
                if not url:
                    logger.error("❌ URL пуст")
                    return None
                
                # Validate URL
                if url.startswith("http://") or url.startswith("https://"):
                    logger.success(f"✅ Переходу на сайт: {url}")
                    return url
                elif "." in url:
                    full_url = f"https://{url}"
                    logger.success(f"✅ Переходу на сайт: {full_url}")
                    return full_url
                else:
                    logger.error(f"❌ Некорректный URL: {url}")
                    return None
            
            # Type 3: Search
            elif response_type == "search":
                query = response_json.get("query", "").strip()
                reason = response_json.get("reason", "")
                
                if not query:
                    logger.error("❌ Поисковый запрос пуст")
                    return None
                
                search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
                logger.success(f"✅ Google поиск: '{query}'")
                return search_url
            
            else:
                logger.error(f"❌ Неизвестный тип ответа: '{response_type}'")
                return None
        
        except Exception as e:
            logger.error(f"❌ Ошибка при интерпретации команды: {str(e)}")
            return None

    async def _compute_page_fingerprint(self) -> str:
        """
        🖐️ Compute a fingerprint of current page state for change detection.
        Combines URL, page title, and hash of visible text to create a unique state signature.
        
        Returns:
            String hash representing page state
        """
        try:
            # Get URL, title, and count of elements as quick state indicator
            url = self.page.url
            title = await self.page.title()
            
            # Get count of interactive elements (faster than hashing all text)
            element_count = await self.page.evaluate("() => document.querySelectorAll('button, a, input').length")
            
            # Create state string
            state_string = f"{url}|{title}|{element_count}"
            
            # Hash it for compact representation
            fingerprint = hashlib.md5(state_string.encode()).hexdigest()
            return fingerprint
        except Exception as e:
            logger.debug(f"Error computing page fingerprint: {e}")
            return "unknown"

    async def _has_page_changed(self, current_fingerprint: str) -> bool:
        """
        Check if page state has changed compared to previous iteration.
        
        Args:
            current_fingerprint: Current page fingerprint
            
        Returns:
            True if page state changed, False if same as before
        """
        if self.previous_page_state is None:
            # First check, always consider it as "changed"
            return True
        
        state_changed = current_fingerprint != self.previous_page_state
        
        if state_changed:
            # Reset counter if page changed
            self.page_state_unchanged_count = 0
        else:
            # Increment counter if page didn't change
            self.page_state_unchanged_count += 1
            logger.warning(f"⚠️ Страница не изменилась после действия (повтор #{self.page_state_unchanged_count})")
        
        return state_changed

    async def _execute_iteratively(self) -> Dict[str, Any]:
        """Выполнить задачу итеративно с принятием решений AI"""
        logger.start()
        logger.info("⚙️ Начало автономного выполнения")
        
        # Reset error tracking
        self.error_history = []
        self.last_error = None
        
        try:
            while self.iteration_count < self.max_iterations:
                self.iteration_count += 1
                logger.info(f"🔄 Итерация {self.iteration_count}")
                
                try:
                    # Analyze current page
                    page_analysis = await self.analyzer.analyze()
                    logger.info(f"Страница: {page_analysis.title}")
                    logger.info(f"URL: {page_analysis.url}")
                    
                    # � Compute page fingerprint for state tracking
                    current_fingerprint = await self._compute_page_fingerprint()
                    page_changed = await self._has_page_changed(current_fingerprint)
                    self.previous_page_state = current_fingerprint
                    
                    # �🔒 СПРОСИ МОДЕЛЬ: есть ли на странице капча?
                    has_captcha = await self._check_if_captcha_page(page_analysis.main_text, page_analysis.url)
                    if has_captcha:
                        logger.warning("🔒 Модель обнаружила КАПЧА/2FA на странице")
                        await self.executor.wait_for_user_action("На странице обнаружена КАПЧА. Пожалуйста пройдите проверку вручную")
                        logger.success("✅ Пользователь прошёл проверку, продолжаю анализ")
                        # ⏳ ЖДЁМ ЗАГРУЗКУ СТРАНИЦЫ ПОСЛЕ КАПЧИ!
                        logger.wait("Жду 3с пока страница загрузится после капчи...")
                        await asyncio.sleep(3)
                        # 🔄 ОЧИЩАЕМ ОТПЕЧАТОК СТРАНИЦЫ чтобы не повторять проверку
                        self.previous_page_state = None
                        self.page_state_unchanged_count = 0
                        continue  # Re-analyze page after user completes CAPTCHA
                    
                    # Prepare context for decision making
                    context = self._build_decision_context(page_analysis)
                    
                    # 🎯 ПРОВЕРИМ: может ли быть задача уже выполнена?
                    task_completion = await self._check_if_task_complete(page_analysis)
                    if task_completion.get("is_complete"):
                        logger.success(f"✅ ЗАДАЧА ЗАВЕРШЕНА!")
                        if task_completion.get("result"):
                            logger.success(f"📋 {task_completion.get('result')}")
                        return {
                            "status": "завершена",
                            "iterations": self.iteration_count,
                            "result": task_completion.get("result")
                        }
                    
                    # Ask AI what to do next
                    logger.tool_call("nvidia_api.decide — решить какое действие выполнить на странице")
                    decision = await self._get_ai_decision(context)
                    
                    # ===== LOG MODEL RESPONSE =====
                    import json as json_lib
                    logger.section(f"📋 ПЛАН МОДЕЛИ НА ИТЕРАЦИИ #{self.iteration_count}")
                    
                    # Parse and format decision for logging (without raw response)
                    try:
                        # Try to extract JSON from response
                        json_start = decision.find('{')
                        json_end = decision.rfind('}') + 1
                        if json_start != -1 and json_end > json_start:
                            json_str = decision[json_start:json_end]
                            json_obj = json_lib.loads(json_str)
                            
                            # Log in STRUCTURED format
                            action = json_obj.get('action', '?')
                            strategy = json_obj.get('strategy', '?')
                            args = json_obj.get('args', {})
                            value = json_obj.get('value', '')
                            reason = json_obj.get('reason', '')
                            
                            logger.info(f"🎯 ДЕЙСТВИЕ: {action.upper()}")
                            logger.info(f"📍 СТРАТЕГИЯ: strategy='{strategy}'")
                            
                            # Show how to find element
                            if args:
                                if isinstance(args, dict):
                                    args_str = ", ".join([f'{k}="{v}"' for k, v in args.items()])
                                    logger.info(f"   Параметры: args={{{args_str}}}")
                            
                            # Show what to fill
                            if value:
                                logger.info(f"📝 ЗНАЧЕНИЕ: '{value[:60]}'")
                            
                            # Show FULL reasoning chain
                            if reason:
                                logger.section("🧠 ЦЕПОЧКА МЫШЛЕНИЯ МОДЕЛИ")
                                # Parse the thinking chain if it contains separators
                                if "|" in reason:
                                    parts = reason.split("|")
                                    for part in parts:
                                        part = part.strip()
                                        if part.startswith("Цель:"):
                                            logger.info(f"🎯 ЦЕЛЬ: {part[5:].strip()}")
                                        elif part.startswith("Уже сделано:"):
                                            logger.info(f"✅ УЖЕ СДЕЛАНО: {part[12:].strip()}")
                                        elif part.startswith("Выбираю:"):
                                            logger.info(f"🔍 ВЫБИРАЮ: {part[8:].strip()}")
                                        else:
                                            logger.info(f"💭 {part}")
                                else:
                                    # Fallback for simple reason
                                    logger.info(f"💭 {reason}")
                            
                            logger.section("═" * 60)
                        else:
                            # No JSON found, just show response
                            logger.error(f"❌ Неправильный ответ (не JSON):\n{decision[:200]}")
                    except Exception as e:
                        logger.error(f"❌ Ошибка парсинга ответа: {str(e)}")
                        logger.debug(f"   Raw response: {decision[:200]}")
                    
                    logger.section("⚙️ ВЫПОЛНЕНИЕ ДЕЙСТВИЯ")
                    logger.info(f"   ⏳ Выполняю действие на странице...")
                    # Parse and execute decision
                    action_result = await self._execute_decision(decision, page_analysis)
                    
                    # 📝 ЗАПИСЬ ДЕЙСТВИЯ В ИСТОРИЮ (успешного или нет)
                    try:
                        parsed_decision = DecisionValidator.parse_decision(decision)
                        if parsed_decision[0]:  # is_valid
                            decision_obj = parsed_decision[1]
                            self.failed_actions.append({
                                "action": decision_obj.get('action', 'unknown'),
                                "strategy": decision_obj.get('strategy', 'unknown'),
                                "args": decision_obj.get('args', {}),
                                "element": decision_obj.get('value', '')[:40] if decision_obj.get('value') else '',
                                "reason": decision_obj.get('reason', 'no reason'),
                                "success": not action_result.get("error")
                            })
                    except:
                        pass  # If parsing fails, skip recording
                    
                    if action_result.get("task_complete"):
                        logger.success("✅ Задача выполнена!")
                        if action_result.get("summary"):
                            logger.info(f"📋 {action_result.get('summary')}")
                        return {
                            "status": "завершена",
                            "iterations": self.iteration_count,
                            "final_url": page_analysis.url,
                            "summary": action_result.get("summary", "Задача выполнена успешно")
                        }
                    
                    # Handle user input request - update goal and retry
                    if action_result.get("user_input"):
                        user_input = action_result['user_input']
                        logger.success(f"✅ Получен ввод пользователя: {user_input}")
                        # Update goal with user input for next iteration
                        self.current_task.goal = f"{self.current_task.goal} — конкретный ввод: {user_input}"
                        # Retry loop with updated goal
                        continue
                    
                    # Check for errors and track them
                    if action_result.get("error"):
                        error_msg = action_result['error']
                        logger.warning(f"❌ Ошибка действия: {error_msg}")
                        
                        # Record failed action for next iteration to know what NOT to do
                        if 'action' in action_result:
                            self.failed_actions.append({
                                "action": action_result.get('action', 'unknown'),
                                "strategy": action_result.get('strategy', 'unknown'),
                                "args": action_result.get('args', {}),
                                "element": action_result.get('element_text', 'unknown'),
                                "reason": error_msg
                            })
                        
                        # Check if page state didn't change
                        if self.page_state_unchanged_count >= self.max_unchanged_threshold:
                            logger.error(f"🚨 CIRCUIT BREAKER: Страница не изменилась {self.page_state_unchanged_count} раз подряд")
                            logger.error(f"Похоже, сайт не реагирует на действия. Остановка.")
                            return {
                                "status": "застрял",
                                "iterations": self.iteration_count,
                                "last_error": "Страница не изменяется после действий",
                                "message": "Агент застрял: страница не реагирует на действия"
                            }
                        
                        # Add to error history
                        self.error_history.append(error_msg)
                        if len(self.error_history) > self.max_error_history:
                            self.error_history.pop(0)
                        
                        # Check for circuit breaker condition
                        consecutive_same_errors = 0
                        if self.error_history:
                            current_error = error_msg
                            for i in range(len(self.error_history) - 1, -1, -1):
                                if self.error_history[i] == current_error:
                                    consecutive_same_errors += 1
                                else:
                                    break
                        
                        if consecutive_same_errors >= self.consecutive_error_threshold:
                            logger.error(f"🚨 CIRCUIT BREAKER: Одна и та же ошибка {consecutive_same_errors} раз")
                            logger.error(f"Ошибка: {error_msg}")
                            logger.error(f"Агент застрял. Остановка выполнения.")
                            return {
                                "status": "застрял",
                                "iterations": self.iteration_count,
                                "last_error": error_msg,
                                "message": "Агент застрял: одна и та же ошибка повторяется"
                            }
                    else:
                        # Clear error history on successful action
                        self.error_history = []
                        # Log successful execution summary
                        logger.section("✅ ДЕЙСТВИЕ ВЫПОЛНЕНО УСПЕШНО")
                        logger.success(f"📊 Итерация {self.iteration_count}: действие выполнено")
                        try:
                            import json
                            response_data = json.loads(decision)
                            if isinstance(response_data, dict):
                                action = response_data.get('action', 'unknown')
                                strategy = response_data.get('strategy', 'unknown')
                                logger.info(f"   ✅ {action.upper()} с стратегией {strategy}")
                        except:
                            pass
                        logger.debug(f"   Новый URL: {page_analysis.url}")
                        logger.debug(f"   Заголовок: {page_analysis.title}")
                    
                    # Brief pause before next iteration
                    await asyncio.sleep(0.5)
                    
                except KeyboardInterrupt:
                    raise  # Re-raise to outer handler
                except Exception as e:
                    logger.error(f"Ошибка на итерации {self.iteration_count}: {str(e)}")
                    await asyncio.sleep(1)
        
        except KeyboardInterrupt:
            logger.warning("Итеративное выполнение прервано пользователем (Ctrl+C)")
            return {
                "status": "прервано",
                "iterations": self.iteration_count,
                "message": "Выполнение прервано пользователем"
            }
        
        return {
            "status": "макс_итераций_достигнуто",
            "iterations": self.iteration_count,
            "message": "Максимум итераций достигнут без завершения задачи"
        }
    async def _check_if_task_complete(self, page_analysis: PageAnalysis) -> Dict[str, Any]:
        """
        🎯 Проверить: выполнена ли текущая задача на этой странице?
        
        Args:
            page_analysis: Анализ текущей страницы
            
        Returns:
            {"is_complete": True/False, "result": "описание результата"}
        """
        if not self.current_task:
            return {"is_complete": False}
        
        logger.tool_call("nvidia_api.call — проверить завершена ли задача на текущей странице")
        logger.debug(f"🎯 Проверяю: может ли задача быть выполнена?")
        
        prompt = f"""
ЗАДАЧА АНАЛИЗА: Понять выполнена ли пользовательская задача на ТЕКУЩЕЙ странице.

=== ИСХОДНАЯ ЗАДАЧА ПОЛЬЗОВАТЕЛЯ ===
Цель: {self.current_task.goal}
Описание: {self.current_task.description}
Тип задачи: {self.current_task.task_type}

=== ТЕКУЩАЯ СТРАНИЦА ===
Заголовок: {page_analysis.title}
URL: {page_analysis.url}

=== СОДЕРЖИМОЕ СТРАНИЦЫ ===
{page_analysis.main_text}

═══════════════════════════════════════════════════════════════

КАК ОПРЕДЕЛИТЬ ЧТО ЗАДАЧА "ВЫПОЛНЕНА":

Задача считается ВЫПОЛНЕННОЙ когда пользователь может НЕПОСРЕДСТВЕННО увидеть/получить/использовать результат.

⚠️ ВАЖНО: Результаты поиска или меню — это НЕ выполнение! Нужен КОНКРЕТНЫЙ результат!

═══════════════════════════════════════════════════════════════

КРИТЕРИИ ДЛЯ РАЗНЫХ ТИПОВ ЗАДАЧ:

1️⃣ ИНФОРМАЦИОННЫЕ ЗАДАЧИ (поиск информации):
   ✅ ВЫПОЛНЕНА если:
      - На странице видна информация которую искал (цена, номер, адрес, текст)
      - Информация четко видна и читаема
      - НЕ просто результаты поиска, а конкретная информация
      
   ❌ НЕ ВЫПОЛНЕНА если:
      - На странице только результаты поиска/ссылки
      - Нужно кликнуть ещё на что-то чтобы получить информацию
      - Страница поиска/каталога/меню

2️⃣ МЕДИА ЗАДАЧИ (включить видео, музыку, картинку):
   ✅ ВЫПОЛНЕНА если:
      - Видеоплеер АКТИВЕН и видео ИГРАЕТ 
      - Визуальные признаки:
         * Видно видео с прогресс-баром внизу (временная шкала)
         * Видна кнопка ПАУЗА (не PLAY!) - это значит видео уже воспроизводится
         * Видна длительность видео и текущее время (например "1:23 / 5:45") И НЕ 0:00
         * Видна иконка полного экрана, громкости, качества видео
         * Видно название видео, канала, количество просмотров  
      - На экране видно саму ВИДЕОКАРТИНКУ (не чёрный экран!)
      
   ❌ НЕ ВЫПОЛНЕНА если:
      - На странице результаты ПОИСКА видео (список видео с миниатюрами)
      - Только описание видео написано, плеер НЕ активен
      - Видна кнопка PLAY (это значит видео ПАУЗИРОВАНО)
      - На экране чёрный фон (видео загружается или ошибка)
      - Видна только 1 кнопка (Play) без прогресс-бара и управления

3️⃣ ПОКУПКА / ОФОРМЛЕНИЕ (заказ товара, оплата, добавление в корзину):
   ✅ ВЫПОЛНЕНА если:
      - Товар ДОБАВЛЕН в корзину (видна корзина с товаром внутри, количество, цена)
      - Или: Заказ оформлен (видна страница подтверждения с номером заказа)
      - Или: Товар в корзине виден четко (количество товара в корзине, итоговая цена, кнопка оформления)
      
   ❌ НЕ ВЫПОЛНЕНА если:
      - На странице только описание товара И видна кнопка "Выбрать" (это НЕ добавлен!)
      - На странице каталог товаров с ссылками
      - Корзина пуста или показывает "0 товаров"
      - Еще не нажали "Выбрать" / "Добавить" / "В корзину"

4️⃣ НАВИГАЦИЯ (перейти на сайт, найти ссылку):
   ✅ ВЫПОЛНЕНА если:
      - Пользователь НАХОДИТСЯ на целевом сайте (URL совпадает или близко)
      - На сайте видна нужная информация/раздел
      
   ❌ НЕ ВЫПОЛНЕНА если:
      - На странице результаты поиска сайта
      - Еще не открыт целевой сайт

═══════════════════════════════════════════════════════════════

ПРИМЕРЫ:

⚠️ ЧАСТАЯ ОШИБКА: Модель думает что видение кнопки "Выбрать" / "Добавить" / "В корзину" = 
задача завершена, но это НЕПРАВИЛЬНО! 
Видение КНОПКИ ≠ НАЖАТИЕ КНОПКИ!
Нужно видеть товар УЖЕ В КОРЗИНЕ!

═══════════════════════════════════════════════════════════════

✅ ВЕРНЫЕ ОТВЕТЫ (задача ВЫПОЛНЕНА):
   
   Задача: "включи видео онлайн"
   Страница: Видео платформа с активным плеером
   ✅ yes: Видеоплеер активен, видео воспроизводится
   
   Задача: "найди цену товара"
   Страница: E-commerce сайт с товаром
   ✅ yes: На странице указана цена товара, видна кнопка "Buy" или "Заказать"
   
   Задача: "посмотри информацию в целевом месте"
   Страница: Целевой сайт с информацией
   ✅ yes: На странице отображается требуемая информация

❌ НЕВЕРНЫЕ ОТВЕТЫ (задача НЕ ВЫПОЛНЕНА):
   
   Задача: "включи видео онлайн"
   Страница: Видео платформа с результатами ПОИСКА (много видео в списке)
   ❌ Неправильный ответ: yes: На странице есть видео
   ✅ Правильный ответ: no (результаты поиска, видео не включено)
   
   Задача: "положи в корзину"
   Страница: Страница с заказом, описанием и кнопкой "Выбрать"
   ❌ Неправильный ответ: yes: Виден заказ и кнопка "Выбрать"
   ✅ Правильный ответ: no (заказ НЕ добавлен в корзину, видна только кнопка, нужно её нажать!)
   
   Задача: "закажи обед в ресторане"
   Страница: Ресторан каталог
   ❌ Неправильный ответ: yes: На странице видны блюда
   ✅ Правильный ответ: no (каталог, блюдо не заказано, нужно выбрать и оплатить)

═══════════════════════════════════════════════════════════════

ТВОЙ ОТВЕТ:

Проанализируй задачу и страницу. Напиши ТОЛЬКО:
- yes: <ОПИСАНИЕ что именно пользователь видит/получил>
- no

БЕЗ лишних объяснений! ТОЛЬКО yes или no с описанием!
"""
        
        try:
            response = await self.api.call_async(prompt, use_history=False)
            if not response:
                logger.debug(f"⚠️ API вернул пустой ответ при проверке завершения")
                return {"is_complete": False}
            response_lower = response.lower().strip()
            
            # ===== PARSE RESPONSE =====
            # Check if answer starts with "yes" (may have description after colon)
            is_complete = False
            result_text = ""
            
            if response_lower.startswith("yes"):
                is_complete = True
                # Extract description if it exists (format: "yes: description")
                if ":" in response_lower:
                    result_text = response.split(":", 1)[1].strip()
                else:
                    result_text = response[3:].strip()  # Remove "yes" and get rest
            elif response_lower.startswith("no"):
                is_complete = False
                # Extract explanation if exists (format: "no: explanation")
                if ":" in response_lower:
                    result_text = response.split(":", 1)[1].strip()
                else:
                    result_text = response[2:].strip()
            else:
                logger.warning(f"⚠️ Ответ модели не начинается с yes/no: {response[:50]}")
                is_complete = False
            
            if is_complete:
                logger.success(f"✅ Задача завершена: {result_text[:150]}")
                return {
                    "is_complete": True,
                    "result": result_text
                }
            else:
                logger.debug(f"🔄 Продолжаю выполнение. Причина: {result_text[:150] if result_text else 'задача не завершена'}")
                return {"is_complete": False}
                
        except Exception as e:
            logger.debug(f"⚠️ Ошибка при проверке завершения: {e}")
            return {"is_complete": False}
    
    async def _check_if_captcha_page(self, page_text: str, page_url: str) -> bool:
        """
        🔒 Спросит модель: на этой странице капча/2FA/проверка безопасности?
        Модель анализирует текст и URL - она сама определит есть ли капча.
        
        Args:
            page_text: Весь текст со страницы
            page_url: URL страницы
            
        Returns:
            True если модель определила что это капча, False иначе
        """
        logger.info("🔒 Спрашиваю модель: на странице капча/2FA/проверка?")
        logger.debug(f"  📋 URL: {page_url}")
        logger.debug(f"  📄 Текст страницы ({len(page_text)} символов):")
        
        # Вывести первые строки текста
        for line in page_text.split('\n')[:15]:
            if line.strip():
                logger.debug(f"     > {line[:100]}")
        
        prompt = f"""Ты анализируешь если на странице ТРЕБУЕТСЯ ИНТЕРАКТИВНАЯ ПРОВЕРКА ОТ ПОЛЬЗОВАТЕЛЯ прямо сейчас.

=== URL СТРАНИЦЫ ===
{page_url}

=== ТЕКСТ СТРАНИЦЫ ===
{page_text}

КАПЧА / БЛОКИРОВКА (ответь: yes: <ПРИЧИНА>):
- "I'm under attack" или "Please wait while we process your request"
- Интерактивный чекбокс для проверки
- Задача с изображениями (selecting images)
- "Verify you're human" с интерактивным элементом
- "Complete the security challenge"
- 2FA форма (код в SMS/email/app)
- "Unusual activity detected" с требованием действия
- Любые модальные окна безопасности с интерактивными элементами

НЕ КАПЧА (ответь: no):
- Обычные страницы товаров, поиска, новостей
- Логин форма (это вход, не проверка)
- "Контактируйте поддержку" без интерактивной проверки
- IP блокировка с текстом (но БЕЗ интерактивного чекбокса)
- Стандартные формы и контенты

Ответь в формате:
- yes: <КОНКРЕТНАЯ_ПРИЧИНА - какой текст или признак указывает на капчу>
- no

ОБЯЗАТЕЛЬНО укажи конкретный признак если скажешь YES!
"""
        
        try:
            logger.debug(f"  🔹 Отправляю prompt на API...")
            response = await self.api.call_async(prompt, use_history=False)
            if not response:
                logger.debug(f"⚠️ API вернул пустой ответ при проверке капши")
                return False
            response_lower = response.lower().strip()
            
            # ===== PARSE RESPONSE (NO RAW LOGGING) =====
            is_captcha = False
            reason = ""
            
            if response_lower.startswith("yes"):
                is_captcha = True
                # Extract reason if exists (format: "yes: reason")
                if ":" in response_lower:
                    reason = response.split(":", 1)[1].strip()
                else:
                    reason = response[3:].strip()
            elif response_lower.startswith("no"):
                is_captcha = False
                # Extract reason if exists (format: "no: reason")
                if ":" in response_lower:
                    reason = response.split(":", 1)[1].strip()
                else:
                    reason = response[2:].strip()
            else:
                # If doesn't start with yes/no, fallback to old method for compatibility
                is_captcha = "yes" in response_lower
                reason = response[:100]
            
            if is_captcha:
                logger.warning(f"🔒 ОБНАРУЖЕНА КАПЧА: {reason[:100]}")
                return True
            else:
                logger.info(f"✅ На странице нет капчи")
                return False
                
        except Exception as e:
            logger.error(f"Ошибка при спросе о капче: {e}")
            return False
    
    def _build_decision_context(self, page_analysis: PageAnalysis) -> str:
        """
        Подготовить контекст для LLM.
        Показываем МИНИМАЛЬНО необходимую информацию чтобы модель приняла решение.
        """
        # ========== LOG WHAT MODEL WILL SEE ==========
        logger.section("📋 КОНТЕКСТ ДЛЯ МОДЕЛИ")
        logger.info("=== ЧТО ВИДИТ МОДЕЛЬ НА СТРАНИЦЕ ===")
        
        # ⭐ Show current task/goal clearly
        logger.warning(f"📌 ТЕКУЩАЯ ЦЕЛЬ МОДЕЛИ: {self.current_task.goal}")
        logger.info(f"   (полная задача: {self.current_task.description})")
        
        # Show title and URL
        logger.info(f"Страница: {page_analysis.title}")
        logger.info(f"URL: {page_analysis.url}")
        
        # Show what has been done already
        if self.iteration_count > 1:
            logger.info(f"=== ЧТО УЖЕ СДЕЛАНО (итерация {self.iteration_count}) ===")
            if self.failed_actions:
                logger.info(f"Попыток выполнено: {len(self.failed_actions)}")
                for i, failed in enumerate(self.failed_actions[-3:], 1):  # Show last 3
                    logger.debug(f"   {i}. {failed['action'].upper()} → {failed['reason'][:60]}")
            else:
                logger.info(f"Выполнено {self.iteration_count - 1} итераций успешно")
        
        # Show main text
        if page_analysis.main_text:
            text_preview = page_analysis.main_text[:400]
            if len(page_analysis.main_text) > 400:
                text_preview += f"\n... ({len(page_analysis.main_text)} символов всего)"
            logger.debug(f"ТЕКСТ СТРАНИЦЫ (видит пользователь):\n{text_preview}")
        else:
            logger.warning("Текст страницы: (пусто)")
        
        # Show modal detection STATUS
        if page_analysis.modal_open:
            logger.warning(f"⚠️  МОДАЛЬНОЕ ОКНО ОТКРЫТО!")
            logger.warning(f"    Текст модали: {page_analysis.modal_text[:80]}")
        else:
            logger.success(f"✅ Модальное окно НЕ обнаружено")
        
        # Show INPUT FIELDS separately (if they exist)
        input_fields = [h for h in page_analysis.search_hints if "FILL:" in h or "INPUT FIELDS:" in h]
        if input_fields:
            logger.section("🎯 ПОЛЯ ДЛЯ ЗАПОЛНЕНИЯ (INPUT FIELDS)")
            for hint in input_fields:
                logger.warning(f"   {hint}")
        else:
            logger.warning("⚠️  НЕ НАЙДЕНЫ INPUT ПОЛЯ (требуется ввод текста, но поля не обнаружены!)")
        
        # Show search hints CLEARLY
        logger.info("=== ДОСТУПНЫЕ ЭЛЕМЕНТЫ (search_hints) ===")
        if page_analysis.search_hints:
            logger.info(f"🔍 Найдено {len(page_analysis.search_hints)} доступных элементов:")
            for i, hint in enumerate(page_analysis.search_hints, 1):
                # Highlight important hints
                if "MODAL" in hint.upper() or "ВАЖНО" in hint.upper():
                    logger.warning(f"   [{i}] {hint}")
                else:
                    logger.info(f"   [{i}] {hint}")
        else:
            logger.warning("   (элементы не найдены - может быть динамический контент или проблема с анализом)")
        
        logger.section("═" * 60)
        
        # ========== BUILD CONTEXT FOR LLM ==========
        context = f"""
╔════════════════════════════════════════════════════════════════╗
║                 ИНСТРУКЦИЯ ДЛЯ МОДЕЛИ                          ║
╚════════════════════════════════════════════════════════════════╝

📝 ИСХОДНАЯ ЗАДАЧА ПОЛЬЗОВАТЕЛЯ:
"{self.current_task.description}"

🎯 ЦЕЛЬ КОТОРУЮ НУЖНО ДОСТИЧЬ:
"{self.current_task.goal}"

════════════════════════════════════════════════════════════════

📊 ПОЛНАЯ ИСТОРИЯ ДЕЙСТВИЙ (что было сделано):

Итерация: {self.iteration_count}
"""
        
        # Build full action history
        if not self.failed_actions:
            context += "Начало выполнения - никаких действий ещё не было\n"
            logger.info("   (никаких действий ещё не было)")
        else:
            successful_count = len([a for a in self.failed_actions if a.get('success', False) == True])
            failed_count = len([a for a in self.failed_actions if a.get('success', False) == False])
            context += f"✅ УСПЕШНЫХ действий: {successful_count}\n"
            context += f"❌ ОШИБОК: {failed_count}\n\n"
            context += "Последовательность (от первого к последнему):\n"
            
            logger.info(f"✅ УСПЕШНЫХ: {successful_count} | ❌ ОШИБОК: {failed_count}")
            logger.info("📜 ПОЛНАЯ ИСТОРИЯ:")
            
            for i, action_rec in enumerate(self.failed_actions, 1):
                mark = "❌" if action_rec.get('success') == False else "✅"
                action_str = action_rec['action'].upper()
                element_str = action_rec.get('element', 'unknown')[:40]
                reason_str = action_rec.get('reason', 'no reason')[:70]
                
                context += f"  {i}. {mark} {action_str:<10} | элемент: '{element_str}'\n"
                context += f"      └─ {reason_str}\n"
                
                # Логируем каждое действие для пользователя
                logger.debug(f"   {i}. {mark} {action_str:<10} element='{element_str}'")
                logger.debug(f"      └─ {reason_str}")
        
        context += f"""

════════════════════════════════════════════════════════════════

📄 ТЕКУЩЕЕ СОСТОЯНИЕ СТРАНИЦЫ (итерация {self.iteration_count}):

Заголовок: {page_analysis.title}
URL: {page_analysis.url}

Главный видимый текст:
{page_analysis.main_text[:300]}
{"... (обрезано)" if len(page_analysis.main_text) > 300 else ""}

════════════════════════════════════════════════════════════════

🔧 ДОСТУПНЫЕ ЭЛЕМЕНТЫ ДЛЯ ВЗАИМОДЕЙСТВИЯ:

"""
        
        if page_analysis.search_hints:
            for i, hint in enumerate(page_analysis.search_hints, 1):
                context += f"  [{i}] {hint}\n"
        else:
            context += "  (нет элементов)\n"
        
        # Add form fields section if exists
        if page_analysis.form_fields:
            context += f"\n📋 ПОЛЯ ДЛЯ ВВОДА:\n"
            for field in page_analysis.form_fields:
                hint = field.get("hint", "")
                context += f"  • {hint}\n"
        
        # Critical information about state
        context += f"\n════════════════════════════════════════════════════════════════\n\n"
        
        if page_analysis.modal_open:
            context += f"⚠️  ВАЖНО: МОДАЛЬНОЕ ОКНО ОТКРЫТО!\n"
            context += f"   Текст: {page_analysis.modal_text[:100]}\n"
            context += f"   Нужно закрыть или взаимодействовать с элементами внутри\n\n"
        
        if self.page_state_unchanged_count > 0:
            context += f"🚨 ПОСЛЕДНЕЕ ДЕЙСТВИЕ НЕ СРАБОТАЛО!\n"
            context += f"   Страница не изменилась после: {self.failed_actions[-1]['action'] if self.failed_actions else 'unknown'}\n"
            context += f"   НУЖЕН ДРУГОЙ ПОДХОД или ДРУГОЙ ЭЛЕМЕНТ\n\n"
        
        context += """════════════════════════════════════════════════════════════════

⭐ ИНСТРУКЦИЯ ДЛЯ ВЫБОРА СЛЕДУЮЩЕГО ДЕЙСТВИЯ:

1️⃣  АНАЛИЗИРУЙ ЧТО ПЕРЕД ТОБОЙ:
    - Видишь "⚠️  СПИСОК ДЛЯ ВЫБОРА"? → ВЫБЕРИ ОДИН элемент из списка, НЕ пиши текст!
    - Модальное окно открыто? → ВЗАИМОДЕЙСТВУЙ С ЭЛЕМЕНТАМИ ВНУТРИ
    - Список элементов (кнопки, ссылки)? → ВЫБЕРИ НУЖНЫЙ, НЕ ПИШИ ТЕКСТ
    - Поле ввода (input)? → СМОТРИ его placeholder/label, пиши ОТНОСЯЩИЙСЯ К НЕМУ текст
    - Dropdown? → РАСКРОЙ и ВЫБЕРИ, не пиши текст заново

2️⃣  ЛОГИКА ДЕЙСТВИЙ: 
    - Если есть ВЫБОР (кнопки/ссылки примерно по одной теме) → ВЫБЕРИ
    - Если НУЖНА ИНФОРМАЦИЯ (её нет в доступном списке) → НАЙДИ ПОЛЕ для ввода
    - Поле поиска: СМОТРИ что в списке → ищи АНАЛОГИЧНОЕ в поиске (если список города - ищи город, если товары - ищи товар)
    - Контекст из списка подскажет что нужно писать в поиск!

3️⃣  КОГДА ЧТО ДЕЛАТЬ:
    - НЕ повторяй неработающие действия
    - ПЕРВЫЙ этап: есть список выбора? → выбирай из него
    - ВТОРОЙ этап: нет нужного в списке? → ищи в поле поиска (но ОДНОГО ТИПА с элементами списка!)
    - Если ничего не получается → спроси пользователя (ask_user)
    - Используй элементы ТОЛЬКО из списка выше

4️⃣  ОБЩИЕ ПРАВИЛА:
    - Если перед тобой список похожих элементов → это варианты выбора (ВЫБЕРИ один)
    - Если перед тобой одно поле с placeholder → это для текстового ввода (ВВЕДИ текст)
    - Если модальное окно → сначала ЗАКРОЙ его или ВЫБЕРИ из предложенных опций

════════════════════════════════════════════════════════════════

🔴 ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:

❌ ЗАПРЕЩЕНО:
  • Пиши объяснения перед JSON - ТОЛЬКО JSON!
  • Повторяй действия которые уже не сработали
  • Придумывай элементы которых нет в списке выше
  • Возвращай массив - ТОЛЬКО ОДИН JSON объект
  • Писать ТЕКСТ в элементы если это СПИСОК ВЫБОРА (кнопки, ссылки)
  • Игнорировать список доступных элементов - используй ТОЛЬКО их
  • ⚠️ ПУТАТЬ ТИПЫ: если видишь в списке "Москва, СПб, Якутск" (города) → ищи ГОРОД в поиске, не название товара!

✅ ОБЯЗАТЕЛЬНО:
  • Верни ТОЛЬКО JSON (ничего больше!)
  • Анализируй ТИП элементов (кнопки vs поля ввода vs список выбора)
  • Определи что это: ГОРОДА? ТОВАРЫ? ОПЦИИ? и пиши в поиск ТО ЖЕ
  • Используй КОНТЕКСТ для выбора правильного элемента
  • В поле "reason" объясни почему именно это действие выбрал
  • Если не знаешь → ask_user

════════════════════════════════════════════════════════════════

📋 ФОРМАТ ОТВЕТА:

{
  "action": "click" | "fill" | "type" | "scroll" | "goto" | "wait" | "ask_user" | "confirm_complete",
  "strategy": "text" | "label" | "placeholder" | "id" | "role" | "aria-label",
  "args": { "ключ": "значение" },
  "value": "текст для fill/type (или пусто)",
  "reason": "узкая цепочка: исходная задача есть -> на этапе -> видим элемент -> выбираю его потому что"
}

════════════════════════════════════════════════════════════════

📌 ПРИМЕРЫ ПРАВИЛЬНЫХ ОТВЕТОВ:

1️⃣ ПРОСМОТР ВИДЕО (нажти play на YouTube):
{
  "action": "click",
  "strategy": "aria-label",
  "args": {"aria-label": "Воспроизведение"},
  "value": "",
  "reason": "видеоплеер на странице -> нужно нажать кнопку Play -> нажимаю на Play кнопку"
}

2️⃣ ПОИСК В КАТАЛОГЕ (найти товар по названию):
{
  "action": "fill",
  "strategy": "placeholder",
  "args": {"placeholder": "Поиск товара"},
  "value": "Собака",
  "reason": "каталог товаров открыт -> нужно найти собаку -> вижу поле поиска -> заполняю его названием собаки"
}

3️⃣ ВЫБОР ИЗ СПИСКА (выбрать город):
{
  "action": "click",
  "strategy": "text",
  "args": {"text": "Углич"},
  "value": "",
  "reason": "модаль со списком городов открыта -> нужен Углич -> вижу кнопку Углич в списке -> кликаю"
}

4️⃣ НУЖНА ИНФОРМАЦИЯ (спросить у пользователя):
{
  "action": "ask_user",
  "strategy": "",
  "args": {},
  "value": "",
  "reason": "нужна информация которой нет на странице -> запрашиваю у пользователя"
}

════════════════════════════════════════════════════════════════

ОТВЕТЬ ТОЛЬКО JSON - НИЧЕГО БОЛЬШЕ!
"""
        return context

    async def _get_ai_decision(self, context: str) -> str:
        """Получить решение следующего действия от AI"""
        logger.section("🤖 ВЫЗОВ МОДЕЛИ")
        logger.info("📞 Отправляю запрос к AI...")
        logger.debug(f"📏 Размер контекста: {len(context)} символов")
        
        # Log what we're asking the model
        lines = context.split('\n')
        for line in lines[:20]:  # Show first lines of context
            if line.strip():
                logger.debug(f"   {line[:100]}")
        
        logger.info("⏳ Жду ответа модели...")
        response = await self.api.decide_async(context)
        logger.success(f"✅ Получен ответ ({len(response)} символов)")
        
        return response


    async def _execute_decision(self, decision: str, page_analysis: PageAnalysis) -> Dict[str, Any]:
        """
        v2: Parse и execute decision from LLM with NEW model
        
        NEW in v2:
        - LLM provides strategy + args instead of elem_id
        - ActionExecutor returns Dict with status (not bool)
        - Handle strict_mode_violation by asking LLM for disambiguation
        """
        # ========== PARSE DECISION (STRICT JSON) ==========
        is_valid, parsed, parse_error = DecisionValidator.parse_decision(decision)
        if not is_valid:
            logger.error(f"❌ Ошибка парсинга решения: {parse_error}")
            logger.error(f"Исходное решение:\n{decision[:200]}")
            return {"error": f"Некорректное решение от LLM: {parse_error}"}
        
        action = (parsed.get("action") or "").lower().strip()
        strategy = (parsed.get("strategy") or "").lower().strip()  # NEW: strategy instead of target
        args = parsed.get("args", {})  # NEW: locator args
        value = parsed.get("value", "")
        reason = parsed.get("reason", "")
        
        # ========== AUTO-BUILD ARGS FROM STRATEGY + VALUE IF ARGS EMPTY ==========
        # Если модель отправила strategy и value, но args пусто - построим args автоматически
        if not args and strategy and value:
            logger.debug(f"🔨 AUTO-BUILD: args был пуст, строю из strategy={strategy} + value={value[:40]}")
            args = {strategy: value}
            logger.debug(f"   Результат: args={args}")
        
        logger.info(f"📋 Решение: ACTION={action}, STRATEGY={strategy}")
        logger.debug(f"   Args: {args}")
        if value:
            logger.debug(f"   Value: {value[:50]}")
        if reason:
            logger.debug(f"   Reason: {reason}")
        
        # ========== VALIDATE DECISION (FIX COMMON ERRORS) ==========
        # Check for common mistakes in strategy/args:
        
        # 0. CHECK IF REPEATING SAME FAILED ACTION - STRICT BLOCK!
        if self.page_state_unchanged_count > 0 and self.failed_actions:
            last_failed = self.failed_actions[-1]
            last_failed_action = last_failed.get('action', '')
            last_failed_element = last_failed.get('element', '')
            
            # Build current action signature
            current_signature = f"{action}:{strategy}:{str(args)}"
            last_failed_signature = f"{last_failed_action}:{last_failed.get('strategy', '')}:{str(last_failed.get('args', {}))}"
            
            if current_signature == last_failed_signature:
                logger.error(f"🚫 ЗАПРЕЩЕНО! Модель пытается повторить то же действие!")
                logger.error(f"   Последнее действие: {last_failed_signature}")
                logger.error(f"   Текущее решение: {current_signature}")
                logger.error(f"   Это не сработало {self.page_state_unchanged_count} раз!")
                logger.error(f"   Возвращаю ошибку вместо выполнения")
                return {
                    "error": "BLOCKED_REPEATED_ACTION", 
                    "details": f"Попытка повторить неудачное действие: {last_failed_action}",
                    "action": action,
                    "strategy": strategy,
                    "args": args,
                    "element_text": str(args)
                }
        
        # 1. If strategy is "text" and args["text"] contains "[aria-label]", it's wrong!
        if strategy == "text" and args.get("text", "").startswith("[aria-label]"):
            logger.warning(f"⚠️ ОШИБКА ВЫБОРА: текст содержит маркер [aria-label]!")
            logger.warning(f"   Старое значение: {args['text']}")
            # Extract aria-label value
            aria_value = args["text"].replace("[aria-label]", "").strip()
            logger.warning(f"   Исправляю на: strategy='aria-label', aria-label='{aria_value}'")
            strategy = "aria-label"
            args = {"aria-label": aria_value}
        
        # 2. If strategy is "text" and args["text"] contains "[id]", it's wrong!
        if strategy == "text" and args.get("text", "").startswith("[id]"):
            logger.warning(f"⚠️ ОШИБКА ВЫБОРА: текст содержит маркер [id]!")
            logger.warning(f"   Старое значение: {args['text']}")
            # Extract id value
            id_value = args["text"].replace("[id]", "").strip()
            logger.warning(f"   Исправляю на: strategy='id', id='{id_value}'")
            strategy = "id"
            args = {"id": id_value}
        
        # ========== SECURITY GATE FOR RISKY ACTIONS ==========
        if self.current_task.is_risky and action in ["submit", "click", "confirm_complete"]:
            risk_keywords = ["оплат", "платёж", "pay", "confirm", "удали", "delete", "отправить"]
            is_risky_action = any(
                kw in (strategy or "").lower() or kw in (reason or "").lower()
                for kw in risk_keywords
            )
            if is_risky_action:
                logger.security_prompt(f"Действие требует подтверждения: {action}")
                if not logger.confirm(f"Вы уверены, что хотите выполнить это действие?"):
                    logger.warning("⚠️ Действие отменено пользователем (безопасность)")
                    return {"error": "Действие отменено пользователем (безопасность)"}
        
        # ========== EXECUTE ACTION ==========
        logger.action(f"Выполняю: {action}")
        logger.indent()
        
        try:
            result = None
            
            if action == "click":
                # Action: CLICK with strategy
                logger.info(f"Клик используя strategy='{strategy}'")
                result = await self.executor.click(
                    locator_strategy=strategy,
                    locator_args=args,
                    element_text=args.get("name", "")
                )
                
                # Handle multiple_matches OR strict_mode_violation
                if result.get("error") in ["strict_mode_violation", "multiple_matches"]:
                    error_count = result.get('count', '?')
                    logger.warning(f"⚠️ НАЙДЕНО {error_count} ЭЛЕМЕНТОВ - нужна уточнение")
                    
                    # Build list of button hints for context
                    button_hints = ""
                    if page_analysis and page_analysis.search_hints:
                        for hint in page_analysis.search_hints:
                            if "[SUBMIT]" in hint or "button" in hint.lower():
                                button_hints += f"• {hint}\n"
                    
                    # Ask LLM for disambiguation with CONTEXT
                    variants = result.get("variants", [])
                    variant_text = "\n".join([
                        f"  Вариант {v.get('index', i)}: {v.get('text', v)}"
                        for i, v in enumerate(variants[:3])
                    ])
                    
                    disambig_prompt = f"""
На странице найдено {error_count} КНОПОК с параметрами strategy='{strategy}', args={args}

ВАРИАНТЫ найденных кнопок:
{variant_text if variant_text.strip() else "(информация о вариантах недоступна)"}

ДОСТУПНЫЕ КНОПКИ на странице (из анализа):
{button_hints if button_hints.strip() else "(информация о доступных кнопках отсутствует)"}

═════════════════════════════════════════════════════

ЗАДАЧА: Вернуть ОДНО решение (не массив!) с точными параметрами для выбора ОДНОГО элемента.

Подходы:
1. Если видишь элемент с [SUBMIT] в названии — используй его текст как полное значение для "text"
2. Если это кнопка поиска — используй текст/aria-label которая указана рядом с input полем
3. Добавить "exact": True для точного совпадения текста
4. Или использовать other параметры (role, title, aria-label и т.д.)

ОБЯЗАТЕЛЬНО:
- Используй точный текст/параметры который РЕАЛЬНО ЕСТЬ на странице
- Не придумывай новые значения!
- Ответ должен быть JSON ОБЪЕКТ (не массив!)

Ответ ТОЛЬКО JSON (ОДИН объект, БЕЗ МАССИВА):
{{
  "strategy": "role|text|label|placeholder|title",
  "args": {{"key": "value"}},
  "reason": "почему выбран этот элемент"
}}
"""
                    
                    try:
                        disambig_response = await self.api.call_async(disambig_prompt)
                        if not disambig_response:
                            logger.debug(f"  ⚠️ Пустой ответ при уточнении элемента (1437)")
                            return {"error": "empty_disambig_response"}
                        disambig_json = json.loads(disambig_response.strip())
                        
                        # Validate that it's not an array
                        if isinstance(disambig_json, list):
                            logger.error(f"❌ LLM вернула array вместо object, берем первый")
                            disambig_json = disambig_json[0] if disambig_json else {}
                        
                        if not disambig_json or not isinstance(disambig_json, dict):
                            logger.error(f"❌ LLM ответила некорректно")
                            return {"error": "disambig_response_invalid"}
                        
                        # Retry with disambiguated strategy
                        logger.info(f"🔄 Повтор с уточненной стратегией")
                        result = await self.executor.click(
                            locator_strategy=disambig_json.get("strategy", strategy),
                            locator_args=disambig_json.get("args", args),
                            allow_multiple=True  # Use first if still multiple
                        )
                    except Exception as disambig_error:
                        logger.error(f"❌ Ошибка при уточнении: {str(disambig_error)[:80]}")
                        return {"error": "strict_mode_disambiguation_failed"}
                
            elif action == "fill":
                logger.info(f"Заполняю используя strategy='{strategy}' = '{value[:30]}'")
                result = await self.executor.fill(
                    locator_strategy=strategy,
                    locator_args=args,
                    text=value,
                    element_text=args.get("label", "")
                )
                
                # Handle strict_mode_violation similar to click
                if result.get("error") == "strict_mode_violation":
                    logger.warning(f"⚠️ STRICT MODE: Найдено {result.get('count')} полей")
                    
                    disambig_prompt = f"""
На странице найдено {result.get('count')} input полей с параметрами:
- strategy: {strategy}
- args: {args}

НЕОБХОДИМО:
Вернуть ОДНО решение (не массив!) с более точными параметрами для выбора ОДНОГО поля.

Используй:
- Более точный placeholder или label
- Добавить фильтр по родительскому элементу
- Использовать nth() если была конкретная позиция

Ответ ТОЛЬКО JSON (ОДИН объект):
{{
  "strategy": "role|text|label|placeholder",
  "args": {{"key": "value"}},
  "reason": "объяснение"
}}
"""
                    try:
                        disambig_response = await self.api.call_async(disambig_prompt)
                        if not disambig_response:
                            logger.debug(f"  ⚠️ Пустой ответ при уточнении элемента (1494)")
                            return {"error": "empty_disambig_response"}
                        disambig_json = json.loads(disambig_response.strip())
                        
                        # Validate response
                        if isinstance(disambig_json, list):
                            disambig_json = disambig_json[0] if disambig_json else {}
                        
                        if not disambig_json or not isinstance(disambig_json, dict):
                            logger.error(f"❌ LLM ответила некорректно")
                            return {"error": "fill_disambig_failed"}
                        
                        result = await self.executor.fill(
                            locator_strategy=disambig_json.get("strategy", strategy),
                            locator_args=disambig_json.get("args", args),
                            text=value
                        )
                    except Exception as disambig_error:
                        logger.error(f"❌ Ошибка при уточнении поля: {str(disambig_error)[:80]}")
                        return {"error": "field_disambiguation_failed"}
                
            elif action == "type":
                logger.info(f"Ввожу в field используя strategy='{strategy}'")
                result = await self.executor.type_text(
                    locator_strategy=strategy,
                    locator_args=args,
                    text=value
                )
                
            elif action == "submit":
                # Submit = click на кнопку
                logger.info(f"Отправляю форму кликом")
                result = await self.executor.click(
                    locator_strategy=strategy,
                    locator_args=args
                )
                
            elif action == "goto":
                logger.navigation(f"Переходу на: {value}")
                success = await self.executor.goto(value)
                result = {"success": success}
                
            elif action == "scroll":
                direction = (value or "down").lower()
                logger.info(f"Прокручиваю: {direction}")
                success = await self.executor.scroll(direction)
                result = {"success": success}
                
            elif action == "wait":
                wait_ms = int(value or 1000)
                logger.wait(f"Жду {wait_ms}мс")
                await self.executor.wait_for_timeout(wait_ms)
                result = {"success": True}
                
            elif action == "ask_user":
                user_prompt = reason or value or "Введите данные:"
                logger.warning(f"❓ {user_prompt}")
                user_answer = logger.ask_user(user_prompt)
                logger.dedent()
                return {
                    "user_input": user_answer,
                    "needs_retry": True
                }
            
            elif action == "wait_for_user_action":
                wait_reason = reason or "Требуется действие пользователя"
                await self.executor.wait_for_user_action(wait_reason)
                logger.dedent()
                return {"success": True}
            
            elif action == "press_key":
                # Нажать клавишу (Enter, Escape и т.д.)
                key_name = value or "Enter"
                logger.info(f"Нажимаю клавишу: {key_name}")
                try:
                    await self.page.keyboard.press(key_name)
                    logger.success(f"✅ Клавиша '{key_name}' нажата")
                    result = {"success": True, "key": key_name}
                    await asyncio.sleep(1)  # Wait for page to process
                except Exception as e:
                    logger.error(f"❌ Ошибка при нажатии клавиши '{key_name}': {e}")
                    result = {"error": str(e)}
                
            elif action == "confirm_complete":
                summary = value or "Задача завершена успешно"
                logger.success(f"✅ Задача завершена: {summary}")
                logger.dedent()
                return {
                    "task_complete": True,
                    "summary": summary
                }
            
            logger.dedent()
            
            # Check result from action
            if result and result.get("success"):
                await asyncio.sleep(max(0.5, self.agent_config.get("page_timeout", 1000) / 1000))
                return {"success": True}
            elif result and result.get("error"):
                error_msg = result.get("reason", result.get("error", "Unknown error"))
                logger.error(f"❌ Ошибка действия: {error_msg[:80]}")
                return {"error": error_msg}
            else:
                return {"error": f"Действие {action} не выполнено"}
                
        except Exception as e:
            logger.error(f"❌ Ошибка выполнения: {str(e)[:100]}")
            logger.dedent()
            return {"error": f"Ошибка выполнения действия: {str(e)[:50]}"}

    async def _check_if_search_results_relevant(self, page_text: str, task_description: str) -> bool:
        """
        Check if Google search results look relevant to the task.
        
        Returns:
            True if results seem relevant, False if we should try different search
        """
        try:
            # Ask LLM to evaluate if search results are relevant
            check_prompt = f"""
Задача пользователя: {task_description[:100]}

Текст результатов поиска на Google:
{page_text[:800]}

Вопрос: Выглядят ли результаты релевантными для выполнения задачи? Ищет ли Google правильное?

Ответ ТОЛЬКО JSON (без ```):
{{
  "is_relevant": true или false,
  "reason": "краткое объяснение"
}}
"""
            
            response = await self.api.call_async(check_prompt, use_history=False)
            if not response:
                logger.debug(f"⚠️ API вернул пустой ответ при проверке релевантности")
                return False
            result_json = json.loads(response.strip())
            
            is_relevant = result_json.get("is_relevant", False)
            reason = result_json.get("reason", "")
            
            if is_relevant:
                logger.success(f"✅ Результаты релевантны: {reason}")
            else:
                logger.warning(f"⚠️ Результаты нерелевантны: {reason}")
            
            return is_relevant
            
        except Exception as e:
            logger.warning(f"⚠️ Ошибка при проверке релевантности: {str(e)[:60]}")
            # Assume relevant if we can't check
            return True

