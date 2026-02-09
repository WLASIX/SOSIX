"""
Page analyzer module.
Analyzes web page structure and finds interactive elements.
Provides structured page representation without raw HTML.
"""
from typing import List, Dict, Any, Optional
from playwright.async_api import Page, ElementHandle, Locator
from logger import logger
import json
import asyncio


class InteractiveElement:
    """
    Представляет интерактивный элемент на странице.
    
    ⚠️ ВАЖНО: Свойства can_click/can_fill/can_type - только ОЖИДАНИЕ на основе ARIA-роли.
    Так как Playwright делает собственные actionability checks (видимость, enabled, events и т.д.),
    эти свойства могут быть неточны. Настоящая проверка происходит в action_executor
    когда Playwright выполняет действие и может выбросить исключение.
    
    Основная информация - это locator_strategy + locator_args,
    которые используются для построения Playwright locator.
    """
    
    def __init__(self, element_id: str, element_type: str, text: str, 
                 selector: str, description: str = ""):
        self.id = element_id
        self.type = element_type  # button, link, input, select, textarea, checkbox, radio, etc.
        self.text = text  # Видимый текст элемента для пользователя
        self.selector = selector  # CSS selector (используется только для справки)
        self.description = description
        
        # 🎯 ГЛАВНОЕ: ИНФОРМАЦИЯ ЛОКАТОРА
        # Это то, что используется в action_executor для построения Playwright locator
        self.locator_strategy: Optional[str] = None  # "role" | "text" | "placeholder" | "css"
        self.locator_args: Dict[str, Any] = {}  # Параметры для стратегии локатора
        
        # ⚠️ ПРОГНОЗ: Что ДОЛЖНО быть возможно на основе ARIA-роли
        # (но может не быть возможно на практике из-за actionability checks Playwright)
        self.can_click: bool = False
        self.can_fill: bool = False
        self.can_type: bool = False
        
        # 📋 Диагностка: Почему элемент не интерактивный
        self.disabled_reason: Optional[str] = None  # "disabled", "readonly", "hidden", etc.
        
        # Метаинформация
        self.role: Optional[str] = None  # ARIA role (например, "button", "link", "textbox")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "text": self.text,
            "description": self.description,
            "locator_strategy": self.locator_strategy,
            "locator_args": self.locator_args,
            "can_click": self.can_click,
            "can_fill": self.can_fill,
            "can_type": self.can_type,
            "disabled_reason": self.disabled_reason,
            "role": self.role
        }
    
    def get_llm_description(self) -> str:
        """
        Описание для LLM - какие действия можно сделать с этим элементом.
        
        LLM получит этот текст для понимания:
        - Что это за элемент
        - Какой у него ID для reference
        - Какие действия с ним можно сделать (ОЖИДАНИЕ для LLM)
        """
        parts = [f"[{self.id}]"]
        
        # Добавить тип
        if self.type != "unknown":
            parts.append(self.type.upper())
        
        # Добавить текст
        if self.text:
            parts.append(f"'{self.text}'")
        
        # Возможные действия (ОЖИДАНИЕ, не гарантия)
        capabilities = []
        if self.can_click:
            capabilities.append("CLICK")
        if self.can_fill:
            capabilities.append("FILL")
        if self.can_type:
            capabilities.append("TYPE")
        
        if capabilities:
            parts.append(f"({', '.join(capabilities)})")
        
        return " ".join(parts)


class PageAnalysis:
    """Analysis result for a web page - SEMANTIC SUMMARY, not all elements"""
    
    def __init__(self):
        self.url: str = ""
        self.title: str = ""
        self.main_text: str = ""  # Full visible text
        
        # v2: НЕ собираем все элементы, собираем hints как их найти
        self.interactive_elements: List[InteractiveElement] = []  # Only for backward compat, kept empty in new mode
        
        # Новое: Подсказки для LLM как найти элементы (вместо списка)
        self.search_hints: List[str] = []  # ["You can click on button 'Submit'", "There are 5 form fields", ...]
        
        self.headings: List[Dict[str, str]] = []
        self.form_fields: List[Dict[str, Any]] = []  # Key form fields info
        self.current_state: Dict[str, Any] = {}
        
        # Modal window detection
        self.modal_open: bool = False
        self.modal_text: str = ""
        self.modal_elements: List[InteractiveElement] = []  # Still empty in v2
        self.modal_close_element: Optional[InteractiveElement] = None
        
        # 🎥 VIDEO ERROR DETECTION (YouTube)
        self.video_error: Optional[str] = None  # "error_tooltip", "reload_needed", "unavailable", etc.
        
    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "main_text": self.main_text,
            "search_hints": self.search_hints,
            "headings": self.headings,
            "form_fields": self.form_fields,
            "current_state": self.current_state,
            "modal_open": self.modal_open,
            "modal_text": self.modal_text,
            "video_error": self.video_error
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


class PageAnalyzer:
    """Analyzes web page structure and content"""

    def __init__(self, page: Page):
        self.page = page
        self.element_counter = 0

    async def analyze(self) -> PageAnalysis:
        """
        v2: Анализировать текущую страницу СЕМАНТИЧЕСКИ
        
        НЕ собираем все элементы (что приводит к 500+ elem_id и strict mode violations).
        Вместо этого:
        1. Извлекаем видимый текст (main_text)
        2. Возвращаем подсказки KAK найти элементы (search_hints)
        3. Готовим основную информацию для LLM
        
        Returns:
            Объект PageAnalysis со структурированными данными
        """
        logger.analysis("Анализирую структуру страницы")
        
        analysis = PageAnalysis()
        analysis.url = self.page.url
        analysis.title = await self._get_title()
        
        # 1. Get main content - ВСЕ видимый текст
        analysis.main_text = await self._get_main_text()
        
        # 2. v2: НЕ собираем элементы, собираем HINTS как их найти
        # interactive_elements остаётся пусто (для новой модели это переписано в ActionExecutor)
        analysis.interactive_elements = []
        analysis.search_hints = await self._get_search_hints()
        
        # 3. Find headings
        analysis.headings = await self._get_headings()
        
        # 4. Detect form fields (ключевые)
        analysis.form_fields = await self._identify_key_form_fields()
        
        # 5. 🚨 DETECT MODAL WINDOWS (ВАЖНО: ДО анализа основного контента!)
        await self._detect_modals(analysis)
        
        # 6. Log page stats
        await self._log_page_stats(analysis)
        
        logger.success(f"Анализ завершен. Найдено {len(analysis.search_hints)} подсказок для действий")
        
        return analysis

    async def _get_title(self) -> str:
        """Get page title"""
        try:
            return await self.page.title()
        except:
            return ""

    async def _get_main_text(self) -> str:
        """
        Получить видимый текст со страницы используя Playwright.
        ВАЖНО: используем Playwright встроенные методы вместо ручного парсинга HTML.
        """
        try:
            # Use Playwright's built-in innerText which respects CSS visibility
            # This is MUCH better than parsing HTML manually
            text = await self.page.evaluate("() => document.body.innerText")
            return text if text else ""
        except Exception as e:
            logger.error(f"Error getting main text: {e}")
            return ""

    async def _check_modal_visible(self) -> bool:
        """
        Быстрая проверка: есть ли видимое модальное окно на странице?
        
        ИСПОЛЬЗУЕТСЯ ЛУЧШИЙ СПОСОБ:
        1. Ищем по role="dialog" и проверяем видимость
        2. Проверяем CSS-селекторы как fallback
        3. Проверяем что элемент действительно видим
        
        Returns:
            True если найдено видимое модальное окно, False в противном случае
        """
        try:
            # ========== СПОСОБ 1: Поиск по ARIA role (САМЫЙ ПРАВИЛЬНЫЙ ПУТЬ) ==========
            # Большинство современных библиотек (React, Vue, Bootstrap) вешают на модалки роль dialog
            # Проверяем видимость каждого найденного диалога
            try:
                # Ищем видимое диалоговое окно
                dialog_locator = self.page.get_by_role("dialog")
                count = await dialog_locator.count()
                
                if count > 0:
                    # Проверяем первый диалог на видимость
                    first_dialog = dialog_locator.first
                    if await first_dialog.is_visible():
                        logger.debug(f"✅ Модальное окно найдено по role='dialog' (найдено {count})")
                        return True
            except Exception as e:
                logger.debug(f"  ⚠️ Ошибка при поиске по role='dialog': {str(e)[:50]}")
                pass
            
            # ========== СПОСОБ 2: Универсальный CSS-селектор (для старых сайтов) ==========
            # Если сайт старый или не следует стандартам доступности
            # Ищем по характерным классам или атрибутам, проверяя видимость
            try:
                # Селектор перебирает частые названия классов и атрибутов
                modal_selector = 'div[class*="modal"], div[class*="popup"], [role="dialog"], .fade.show'
                modal_locator = self.page.locator(modal_selector)
                count = await modal_locator.count()
                
                if count > 0:
                    # Проверяем последний элемент (обычно он поверх всех)
                    last_modal = modal_locator.last
                    if await last_modal.is_visible():
                        logger.debug(f"✅ Модальное окно найдено по CSS селектору (найдено {count})")
                        return True
            except Exception as e:
                logger.debug(f"  ⚠️ Ошибка при поиске по CSS селектору: {str(e)[:50]}")
                pass
            
            # Если ничего не найдено
            logger.debug("✓ Видимое модальное окно не обнаружено")
            return False
            
        except Exception as e:
            logger.debug(f"Ошибка при проверке модального окна: {e}")
            return False
    
    async def _get_modal_locator(self) -> Optional[Locator]:
        """
        Получить локатор видимого модального окна.
        
        ИСПОЛЬЗУЕТСЯ ЛУЧШИЙ СПОСОБ:
        1. Ищем по role="dialog" и проверяем видимость
        2. Fallback на CSS-селекторы
        3. Берем ПОСЛЕДНИЙ элемент (обычно он поверх всех)
        
        Returns:
            Locator модального окна или None если модаль не открыта
        """
        try:
            # ========== МЕТОД 1: Поиск по ARIA role (САМЫЙ НАДЕЖНЫЙ) ==========
            # Большинство библиотек используют role="dialog"
            try:
                dialog_locator = self.page.get_by_role("dialog")
                count = await dialog_locator.count()
                
                if count > 0:
                    # Проверяем видимость первого диалога
                    first_dialog = dialog_locator.first
                    if await first_dialog.is_visible():
                        logger.debug(f"✅ Получен локатор модали по role='dialog' (найдено {count})")
                        return first_dialog
            except Exception as e:
                logger.debug(f"  ⚠️ Ошибка метода 1 (role='dialog'): {str(e)[:50]}")
                pass
            
            # ========== МЕТОД 2: Поиск по CSS классам (для старых сайтов) ==========
            try:
                # Селектор перебирает частые названия классов и атрибутов
                modal_selector = 'div[class*="modal"], div[class*="popup"], [role="dialog"], .fade.show'
                modal_locator = self.page.locator(modal_selector)
                count = await modal_locator.count()
                
                if count > 0:
                    # Берем ПОСЛЕДНИЙ элемент (обычно он поверх всех) и проверяем видимость
                    last_modal = modal_locator.last
                    if await last_modal.is_visible():
                        logger.debug(f"✅ Получен локатор модали по CSS селектору (найдено {count})")
                        return last_modal
            except Exception as e:
                logger.debug(f"  ⚠️ Ошибка метода 2 (CSS селектор): {str(e)[:50]}")
                pass
            
            logger.debug("⚠️ Модальное окно не найдено")
            return None
        except Exception as e:
            logger.debug(f"Ошибка при получении локатора модали: {e}")
            return None

    async def _get_search_hints(self) -> List[str]:
        """
        Возвращает СТРУКТУРИРОВАННЫЙ СПИСОК активных элементов на странице.
        Модель ДОЛЖНА выбирать из этого списка, а не придумывать имена!
        
        ВАЖНЕЙШИЙ ПОРЯДОК:
        1️⃣ INPUT FIELDS (ПЕРВЫЕ!)
        2️⃣ BUTTONS
        3️⃣ LINKS
        ... остальное
        
        ⚠️  ВАЖНО: Если открыто модальное окно, внизу мы вернем ТОЛЬКО элементы модали!
        """
        hints: List[str] = []
        
        try:
            # ========== PRECHECK: Есть ли модальное окно? ==========
            # Проверяем в НАЧАЛО, чтобы потом знать - добавлять ли элементы основной страницы
            modal_window_open = await self._check_modal_visible()
            
            if modal_window_open:
                logger.debug("🚨 Обнаруженного модальное окно - будут выданы ТОЛЬКО его элементы")
            
            # ========== 0. ВИДЕО ПЛЕЕР (простая универсальная детекция) ==========
            # Просто проверяем есть ли видео. Модель сама разберется как с ним работать
            has_video = False
            
            # Если есть модальное окно - пропускаем видео плеер основной страницы
            if not modal_window_open:
                try:
                    # Проверить наличие <video> элемента используя evaluate (Playwright правильный способ)
                    video_count = await self.page.evaluate("() => document.querySelectorAll('video').length")
                    if video_count > 0:
                        has_video = True
                except:
                    pass
                
                if has_video:
                    hints.append("PLAYER: На странице загружен видеоплеер")
                    hints.append("  → Попробуй кликнуть на плеер или нажать пробел для запуска")
                    hints.append("")  # Empty line
            
            # ========== 1️⃣ INPUT FIELDS - ПЕРВЫМИ! (ПЕРЕД КНОПКАМИ!) ==========
            # ВАЖНО: input fields должны быть первыми потому что часто нужно ввести текст ДО нажатия кнопки
            # ТАКЖЕ: ищем поля ВНУТРИ модального окна если оно открыто!
            
            input_info = []
            
            # ========== 1a️⃣ INPUT FIELDS ВНУТРИ МОДАЛЬНОГО ОКНА ==========
            if modal_window_open:
                try:
                    modal_locator = await self._get_modal_locator()
                    if modal_locator:
                        # Ищем input поля ВНУТРИ модали
                        input_locator = modal_locator.locator('input:not([type="hidden"]), textarea, [contenteditable="true"]')
                        modal_inputs = await input_locator.all()
                        logger.debug(f"🔍 INPUT FIELDS ВНУТРИ МОДАЛИ: найдено {len(modal_inputs)} полей")
                        
                        for input_elem in modal_inputs:
                            try:
                                is_visible = await input_elem.is_visible()
                                if not is_visible:
                                    continue
                                
                                placeholder = (await input_elem.get_attribute("placeholder")) or ""
                                aria_label = (await input_elem.get_attribute("aria-label")) or ""
                                
                                # Определяем назначение поля (выбор города, поиск и т.д.)
                                field_context = ""
                                try:
                                    parent_text = await input_elem.evaluate("""
                                        el => {
                                            let text = "";
                                            if (el.labels && el.labels[0]) { text = el.labels[0].innerText; }
                                            if (!text && el.parentElement) { text = el.parentElement.innerText?.split(el.value)[0] || ""; }
                                            return text.trim().substring(0, 100);
                                        }
                                    """)
                                    if parent_text:
                                        field_context = parent_text
                                except:
                                    pass
                                
                                if placeholder:
                                    strategy = "placeholder"
                                    value = placeholder
                                elif aria_label:
                                    strategy = "aria-label"
                                    value = aria_label
                                else:
                                    continue
                                
                                hint_str = f'FILL: {field_context or "поле ввода"} | strategy="{strategy}", args={{"{strategy}": "{value[:40]}"}}'
                                if hint_str not in input_info:
                                    input_info.append(hint_str)
                                    logger.debug(f"   ✅ Найдено поле в модали: {hint_str[:80]}")
                            except:
                                pass
                except Exception as e:
                    logger.debug(f"⚠️  Ошибка при поиске input полей в модали: {str(e)[:50]}")
            
            # ========== 1b️⃣ INPUT FIELDS НА СТРАНИЦЕ (если модали нет) ==========
            if not modal_window_open:
                try:
                    input_locator = self.page.locator('input:not([type="hidden"]), textarea, [contenteditable="true"]')
                    all_inputs = await input_locator.all()
                    logger.debug(f"🔍 INPUT FIELDS на странице: найдено {len(all_inputs)} полей")
                    
                    for input_elem in all_inputs:
                        try:
                            # Проверяем видимость элемента
                            is_visible = await input_elem.is_visible()
                            if not is_visible:
                                continue
                            
                            # Проверяем editable (чтобы не включать read-only поля)
                            is_editable = await input_elem.is_editable()
                            if not is_editable:
                                # Для contenteditable, нужно проверить по-другому
                                try:
                                    is_contenteditable = await input_elem.evaluate("el => el.contentEditable === 'true'")
                                    if not is_contenteditable:
                                        continue
                                except:
                                    continue
                            
                            # 🎯 Собираем данные через нативный Playwright API (надежнее)
                            placeholder = (await input_elem.get_attribute("placeholder")) or ""
                            aria_label = (await input_elem.get_attribute("aria-label")) or ""
                            element_id = (await input_elem.get_attribute("id")) or ""
                            input_type = (await input_elem.get_attribute("type")) or "text"
                            
                            # Получаем связанный <label> если есть
                            label_text = ""
                            try:
                                label_text = await input_elem.evaluate("el => el.labels?.[0]?.innerText || ''")
                            except:
                                pass
                            
                            # Определяем лучшую стратегию для поиска этого поля
                            strategy_to_use = None
                            strategy_value = None
                            
                            # Приоритет: placeholder > aria-label > label > id
                            if placeholder:
                                strategy_to_use = "placeholder"
                                strategy_value = placeholder
                            elif aria_label:
                                strategy_to_use = "aria-label"
                                strategy_value = aria_label
                            elif label_text:
                                strategy_to_use = "label"
                                strategy_value = label_text
                            elif element_id:
                                strategy_to_use = "id"
                                strategy_value = element_id
                            else:
                                # Fallback: try to get the tag name
                                tag_name = await input_elem.evaluate("el => el.tagName.toLowerCase()")
                                if tag_name == "textarea":
                                    strategy_to_use = "role"
                                    strategy_value = "textbox"
                                else:
                                    continue  # Skip if no identifiable attribute
                            
                            # 🎯 Попробуем получить КОНТЕКСТ поля - что рядом?
                            field_context = ""
                            try:
                                # Смотрим текст в родительском контейнере (обычно там лейбл или подсказка)
                                parent_text = await input_elem.evaluate("""
                                    el => {
                                        // Ищем текст рядом с инпутом
                                        let text = "";
                                        // 1. Ищем сам лейбл если он связан
                                        if (el.labels && el.labels[0]) {
                                            text = el.labels[0].innerText;
                                        }
                                        // 2. Если нет - ищем в близком родителе (обычно div с лейблом)
                                        if (!text && el.parentElement) {
                                            text = el.parentElement.innerText?.split(el.value)[0] || "";
                                        }
                                        return text.trim().substring(0, 100);
                                    }
                                """)
                                if parent_text:
                                    field_context = parent_text
                            except:
                                pass
                            
                            # 🎯 Ищем варианты/подсказки которые могли бы быть списком выбора (select, dropdown, autocomplete)
                            options_context = ""
                            try:
                                options_list = await input_elem.evaluate("""
                                    el => {
                                        let options = [];
                                        
                                        // Если это select
                                        if (el.tagName === 'SELECT') {
                                            options = Array.from(el.options).slice(0, 5).map(o => o.text);
                                        }
                                        
                                        // Если это input с datalist
                                        if (el.getAttribute('list')) {
                                            let datalist = document.getElementById(el.getAttribute('list'));
                                            if (datalist) {
                                                options = Array.from(datalist.options || datalist.children)
                                                    .slice(0, 5)
                                                    .map(o => o.text || o.value);
                                            }
                                        }
                                        
                                        return options.filter(o => o).slice(0, 3);
                                    }
                                """)
                                if options_list:
                                    options_context = f" [ВАРИАНТЫ: {', '.join(options_list[:3])}]"
                            except:
                                pass
                            
                            # 🎯 Создаем ИНФОРМАТИВНЫЙ хинт с контекстом
                            if field_context:
                                # Используем контекст если он есть (лейбл, родительский текст)
                                hint_str = f'FILL: {field_context} | strategy="{strategy_to_use}", args={{"{strategy_to_use}": "{strategy_value[:40]}"}} {options_context}'
                            else:
                                # Fallback на базовый формат
                                hint_str = f'FILL: strategy="{strategy_to_use}", args={{"{strategy_to_use}": "{strategy_value[:60]}"}} {options_context}'
                            
                            if hint_str not in input_info:  # Избегаем дубликатов
                                input_info.append(hint_str)
                                logger.debug(f"   ✅ Найдено поле: {hint_str[:100]}")
                        
                        except Exception as e:
                            logger.debug(f"   ⚠️  Ошибка при обработке input элемента: {str(e)[:50]}")
                            pass
                    
                    # Выводим найденные поля
                    if input_info:
                        hints.append("🎯 ЗАПОЛНИ ПОЛЕ (перед кнопками!) используя FILL action:")
                        for input_desc in input_info:
                            hints.append(f'  ➡️  {input_desc} → указать value="<текст для ввода>"')
                        hints.append("")  # Empty line after inputs
                    else:
                        logger.warning("⚠️  НЕ НАЙДЕНЫ INPUT ПОЛЯ на странице!")
                
                except Exception as e:
                    logger.error(f"❌ Ошибка при поиске input полей: {str(e)[:100]}")
            
            # ========== 2️⃣ КНОПКИ - ВТОРОЙ РАЗДЕЛ (ПОСЛЕ INPUT!) ==========
            buttons_count = await self.page.get_by_role("button").count()
            if buttons_count > 0 and not modal_window_open:  # ТОЛЬКО если нет модального окна
                button_list = []
                try:
                    buttons = await self.page.get_by_role("button").all()
                    
                    # Найти input поля поиска один раз
                    search_inputs = []
                    try:
                        searchboxes = await self.page.get_by_role("searchbox").all()
                        search_inputs.extend(searchboxes)
                    except:
                        pass
                    
                    for btn in buttons:
                        # Получить основной текст кнопки (первая строка)
                        main_text = await btn.evaluate("""
                            elem => {
                                let text = elem.innerText || elem.textContent;
                                if (!text) return '';
                                // Take first line only
                                let first_line = text.split('\\n')[0].trim();
                                return first_line;
                            }
                        """)
                        
                        # Получить aria-label, title, ID и кастомные атрибуты
                        button_info = await btn.evaluate("""elem => {
                            let info = {
                                aria_label: elem.getAttribute('aria-label') || '',
                                title: elem.getAttribute('title') || '',
                                id: elem.getAttribute('id') || '',
                                data_attrs: {}
                            };
                            // Собрать все data-* атрибуты
                            for (let attr of elem.attributes) {
                                if (attr.name.startsWith('data-')) {
                                    info.data_attrs[attr.name] = attr.value;
                                }
                            }
                            return info;
                        }""")
                        
                        aria_label = button_info.get('aria_label', '')
                        title_attr = button_info.get('title', '')
                        element_id = button_info.get('id', '')
                        data_attrs = button_info.get('data_attrs', {})
                        
                        # Определить отображаемый текст - приоритет: видимый текст → aria-label → title → ID
                        display_text = main_text.strip() if main_text and main_text.strip() else ""
                        
                        # Если нет видимого текста, использовать aria-label или title
                        if not display_text:
                            if aria_label and aria_label.strip():
                                display_text = f"[aria-label] {aria_label.strip()}"
                            elif title_attr and title_attr.strip():
                                display_text = f"[title] {title_attr.strip()}"
                            elif element_id and element_id.strip():
                                display_text = f"[id] {element_id.strip()}"
                        
                        # Пропустить если совсем нет текста
                        if not display_text:
                            continue
                        
                        cleaned_text = display_text[:80]  # 80 chars max for readability
                        
                        # Проверить: находится ли эта кнопка рядом с input полем поиска?
                        is_search_button = False
                        if search_inputs:
                            try:
                                btn_rect = await btn.bounding_box()
                                if btn_rect:
                                    # Проверить близость к input полям (максимум 200px по горизонтали)
                                    for search_input in search_inputs:
                                        input_rect = await search_input.bounding_box()
                                        if input_rect:
                                            horizontal_distance = abs(btn_rect['x'] - (input_rect['x'] + input_rect['width']))
                                            # Если кнопка находится справа от input в пределах 200px - это кнопка отправки
                                            if horizontal_distance < 200 and btn_rect['y'] >= input_rect['y'] - 20 and btn_rect['y'] <= input_rect['y'] + input_rect['height'] + 20:
                                                is_search_button = True
                                                break
                            except:
                                pass
                        
                        # Добавить в список с пометкой если это кнопка отправки
                        if is_search_button:
                            final_text = f"[SUBMIT] {cleaned_text}"
                        else:
                            final_text = cleaned_text
                        
                        # Добавить информацию о кастомных атрибутах если они есть
                        if data_attrs:
                            attr_str = " ".join([f'{k}="{v}"' for k, v in data_attrs.items()])
                            final_text = f'{final_text} ({attr_str[:60]})'
                        
                        if final_text not in button_list:  # Avoid duplicates
                            button_list.append(final_text)
                except:
                    pass
                
                if button_list:
                    hints.append("BUTTONS (выбери одну из этих кнопок):")
                    for btn_text in button_list:
                        hints.append(f'  • "{btn_text}"')
                else:
                    hints.append(f"(There are {buttons_count} buttons but they have no visible text)")

            
            # ========== 3️⃣ ССЫЛКИ - ТРЕТИЙ РАЗДЕЛ ==========
            links_count = await self.page.get_by_role("link").count()
            if links_count > 0 and not modal_window_open:  # ТОЛЬКО если нет модального окна
                link_list = []
                try:
                    links = await self.page.get_by_role("link").all()
                    for link in links:
                        # Получить ОСНОВНОЙ текст ссылки (первая строка)
                        main_text = await link.evaluate("""
                            elem => {
                                let text = elem.innerText || elem.textContent;
                                if (!text) return '';
                                // Take first line only
                                let first_line = text.split('\\n')[0].trim();
                                return first_line;
                            }
                        """)
                        
                        if main_text and main_text.strip() and len(main_text.strip()) > 2:  # Skip empty or very short
                            # Также попытаться получить дополнительный контекст (платформа, автор)
                            context = await link.evaluate("""
                                elem => {
                                    // Попытаться найти контекст (YouTube, ВКонтакте и т.д.)
                                    let context_text = '';
                                    
                                    // Ищем текст вроде "YouTube ·", "ВКонтакте", "25 мая 2017"
                                    let all_text = (elem.innerText || elem.textContent || '').split('\\n');
                                    
                                    // Обычно контекст во второй и третьей строке
                                    if (all_text.length > 1) {
                                        // Собираем вторую и третью строки как контекст
                                        context_text = all_text.slice(1, 3).join(' · ').trim();
                                    }
                                    
                                    return context_text;
                                }
                            """)
                            
                            cleaned_text = main_text.strip()[:60]  # 60 chars max
                            
                            # Создаём уникальный ключ (текст + контекст)
                            display_text = cleaned_text
                            if context and context.strip() and len(context.strip()) > 2:
                                display_text = f"{cleaned_text} ({context.strip()[:40]})"
                            
                            if display_text not in link_list:  # Avoid duplicates
                                link_list.append(display_text)
                except:
                    pass
                
                if link_list:
                    hints.append("")  # Empty line for readability
                    hints.append("LINKS (выбери одну из этих ссылок):")
                    for link_text in link_list:
                        hints.append(f'  • "{link_text}"')
                else:
                    hints.append(f"(There are {links_count} links but they have no visible text)")
            
            # ========== 4️⃣ ЧЕКБОКСЫ И РАДИО ==========
            checkbox_count = await self.page.get_by_role("checkbox").count()
            radio_count = await self.page.get_by_role("radio").count()
            
            if checkbox_count > 0 and not modal_window_open:
                hints.append(f'There are {checkbox_count} checkboxes')
            
            if radio_count > 0 and not modal_window_open:
                hints.append(f'There are {radio_count} radio buttons')
            
            # ========== 5️⃣ Проверить SELECTS ==========
            select_count = await self.page.get_by_role("combobox").count()
            if select_count > 0 and not modal_window_open:
                hints.append(f'There are {select_count} dropdown selects')
            
            # ========== 5.5️⃣ Проверить LISTBOX (выпадающие меню) ==========
            if not modal_window_open:  # ТОЛЬКО на основной странице
                try:
                    listbox_count = await self.page.get_by_role("listbox").count()
                    if listbox_count > 0:
                        hints.append(f'LISTBOX/DROPDOWN: {listbox_count} меню выбора')
                        
                        # Попробать собрать опции
                        try:
                            options = await self.page.get_by_role("option").all()
                            if options:
                                option_texts = []
                                for opt in options[:15]:  # First 15 options only
                                    try:
                                        opt_text = await opt.text_content()
                                        opt_text = opt_text.strip() if opt_text else ""
                                        
                                        # Собрать кастомные атрибуты
                                        custom_attrs = await opt.evaluate("""elem => {
                                            let attrs = {};
                                            for (let attr of elem.attributes) {
                                                if (attr.name.startsWith('data-') || attr.name === 'value' || attr.name === 'id') {
                                                    attrs[attr.name] = attr.value;
                                                }
                                            }
                                            return attrs;
                                        }""")
                                        
                                        # Форматировать вывод с явным указанием стратегии КЛИКА
                                        if opt_text:
                                            # Если есть ID - покажи как кликать через ID
                                            if 'id' in custom_attrs:
                                                opt_desc = f'CLICK: strategy="id", args={{"id": "{custom_attrs["id"]}"}} → {opt_text[:35]}'
                                            else:
                                                attr_str = " ".join([f'{k}="{v}"' for k, v in custom_attrs.items()])
                                                if attr_str:
                                                    opt_desc = f'{opt_text[:40]} [{attr_str[:50]}]'
                                                else:
                                                    opt_desc = f'CLICK: strategy="text", args={{"text": "{opt_text[:35]}"}} → вариант поиска'
                                            option_texts.append(opt_desc)
                                    except:
                                        pass
                                
                                if option_texts:
                                    hints.append(f'')
                                    hints.append(f'⭐️ РЕЗУЛЬТАТЫ ПОИСКА (нажми на один из них):')
                                    for opt_text in option_texts:

                                        hints.append(f'    • {opt_text}')
                        except:
                            pass
                except:
                    pass
            
            # ========== 6️⃣ Проверить MODAL окна с опциями ==========
            modal_found = False
            
            # Способ 1: Ищем по role=dialog (стандартные модали)
            try:
                dialogs = await self.page.get_by_role("dialog").all()
                for dialog in dialogs:
                    try:
                        is_visible = await dialog.is_visible()
                        if is_visible:
                            modal_found = True
                            # Используем Playwright методы для поиска элементов ВНУТРИ диалога
                            # Это найдет ALL кнопки независимо от тега (<button>, div[role='button'], etc)
                            dialog_buttons = await dialog.get_by_role("button").all()
                            dialog_options = await dialog.get_by_role("option").all()
                            all_elements = dialog_buttons + dialog_options
                            
                            if all_elements:
                                hints.append(f'⚠️  MODAL DIALOG ОТКРЫТА: {len(all_elements)} выбираемых элементов')
                                hints.append(f'   ⚠️  ВАЖНО: Выбери параметры ДО нажатия финальной кнопки!')
                                for elem in all_elements[:15]:
                                    try:
                                        elem_text = await elem.text_content()
                                        elem_text = elem_text.strip() if elem_text else ""
                                        
                                        # Собрать все атрибуты
                                        custom_attrs = await elem.evaluate("""elem => {
                                            let attrs = {};
                                            for (let attr of elem.attributes) {
                                                if (attr.name.startsWith('data-') || attr.name === 'value' || attr.name === 'id') {
                                                    attrs[attr.name] = attr.value;
                                                }
                                            }
                                            return attrs;
                                        }""")
                                        if elem_text or custom_attrs:
                                            attr_str = " ".join([f'{k}="{v}"' for k, v in custom_attrs.items()])
                                            if attr_str:
                                                hints.append(f'  • {elem_text[:40]} [{attr_str[:50]}]')
                                            else:
                                                hints.append(f'  • {elem_text[:50]}')
                                    except:
                                        pass
                    except:
                        pass
            except:
                pass
            
            # Способ 2: Ищем по CSS-классам popup/modal (как Dodo Pizza)
            # Используем evaluate + Playwright для правильного поиска
            if not modal_found:
                try:
                    # Сначала найдем контейнеры с popup/modal в классах через JavaScript
                    popup_elements = await self.page.evaluate("""
                        () => {
                            let popups = [];
                            // Ищем элементы WHERE class содержит 'popup' ИЛИ 'modal'
                            let matching = document.querySelectorAll('[class*="popup"], [class*="modal"]');
                            for (let elem of matching) {
                                // Проверяем видимость через JavaScript
                                let rect = elem.getBoundingClientRect();
                                let isVisible = rect.width > 0 && rect.height > 0 && window.getComputedStyle(elem).display !== 'none';
                                if (isVisible) {
                                    popups.push({
                                        html: elem.outerHTML.substring(0, 100),
                                        class: elem.getAttribute('class')
                                    });
                                }
                            }
                            return popups;
                        }
                    """)
                    
                    # Если нашли popup элементы, работаем с ними через Playwright
                    if popup_elements:
                        # Используем get_by_role чтобы найти кнопки в ВИДИМОМ popup контейнере
                        # Сначала проверяем что есть видимые элементы с классами popup/modal
                        
                        # Ищем ВСЕ видимые кнопки на странице которые находятся внутри popup
                        all_buttons = await self.page.get_by_role("button").all()
                        all_options = await self.page.get_by_role("option").all()
                        all_menuitems = await self.page.get_by_role("menuitem").all()
                        
                        all_elements = all_buttons + all_options + all_menuitems
                        
                        # Фильтруем элементы которые видимы И находятся внутри popup
                        popup_inner_elements = []
                        for elem in all_elements:
                            try:
                                is_visible = await elem.is_visible()
                                if is_visible:
                                    # Проверяем что элемент находится внутри popup контейнера
                                    is_in_popup = await elem.evaluate("""
                                        elem => {
                                            // Проверяем все родители элемента
                                            let parent = elem.parentElement;
                                            while (parent) {
                                                let cls = parent.getAttribute('class') || '';
                                                if (cls.includes('popup') || cls.includes('modal')) {
                                                    return true;
                                                }
                                                parent = parent.parentElement;
                                            }
                                            return false;
                                        }
                                    """)
                                    if is_in_popup:
                                        popup_inner_elements.append(elem)
                            except:
                                pass
                        
                        # Если нашли достаточно элементов в popup (больше чем просто кнопка закрытия)
                        if popup_inner_elements and len(popup_inner_elements) > 2:
                            modal_found = True
                            hints.append(f'')
                            hints.append(f'⚠️  МОДАЛЬНОЕ ОКНО ОТКРЫТО: {len(popup_inner_elements)} интерактивных элементов')
                            hints.append(f'   ⚠️  ВАЖНО: Выбери ВСЕ параметры (размер/тип/добавки) ДО финальной кнопки!')
                            hints.append(f'   Элементы в модали:')
                            
                            for elem in popup_inner_elements[:20]:
                                try:
                                    elem_text = await elem.text_content()
                                    elem_text = elem_text.strip() if elem_text else ""
                                    
                                    # Собрать все атрибуты
                                    custom_attrs = await elem.evaluate("""elem => {
                                        let attrs = {};
                                        for (let attr of elem.attributes) {
                                            if (attr.name.startsWith('data-') || attr.name === 'id' || attr.name === 'class' || attr.name === 'onclick') {
                                                attrs[attr.name] = attr.value;
                                            }
                                        }
                                        return attrs;
                                    }""")
                                    
                                    if elem_text or custom_attrs:
                                        attr_str = " ".join([f'{k}="{v}"' for k, v in custom_attrs.items()])
                                        if attr_str:
                                            hints.append(f'      • {elem_text[:35]} | {attr_str[:55]}')
                                        else:
                                            hints.append(f'      • {elem_text[:50]}')
                                except:
                                    pass
                except:
                    pass
            
            # ========== 6.5️⃣ КНОПКИ ИЗ МОДАЛЬНОГО ОКНА ==========
            # Если модальное окно открыто, ищем ВСЕ ВОЗМОЖНЫЕ КНОПКИ внутри него
            if modal_window_open:
                try:
                    logger.debug("🔍 Ищу ВСЕ кнопки ВНУТРИ модального окна...")
                    modal_locator = await self._get_modal_locator()
                    
                    if modal_locator:
                        # 🎯 Ищем все возможные кнопки: <button>, [role="button"], <a>, submit input и т.д.
                        buttons_locator = modal_locator.locator('button, [role="button"], a[href], input[type="submit"], input[type="button"]')
                        
                        try:
                            # Ждем появления хотя бы одной кнопки (на случай анимации)
                            await buttons_locator.first.wait_for(state="visible", timeout=2000)
                        except:
                            # Если кнопок нет - продолжаем
                            pass
                        
                        # Получаем все найденные кнопки
                        all_buttons = await buttons_locator.all()
                        logger.debug(f"  📊 Всего найдено элементов-кнопок: {len(all_buttons)}")
                        
                        if all_buttons:
                            # 🎯 ОПРЕДЕЛЯЕМ: Это список выбора или отдельные кнопки?
                            # Если более 3 похожих кнопок - вероятно это селектор (город, вариант, и т.д.)
                            is_selection_list = len(all_buttons) > 3
                            
                            if is_selection_list:
                                hints.append("")
                                hints.append("⚠️  СПИСОК ДЛЯ ВЫБОРА (выбери ОДИН элемент, не пиши текст):")
                            else:
                                hints.append("")
                                hints.append("🔴 КНОПКИ И ССЫЛКИ В МОДАЛЬНОМ ОКНЕ:")
                            
                            button_count = 0
                            for idx, btn in enumerate(all_buttons):
                                try:
                                    # Проверяем видимость
                                    is_visible = await btn.is_visible()
                                    if not is_visible:
                                        logger.debug(f"    [{idx}] ⚠️ Невидима - пропускаем")
                                        continue
                                    
                                    # Получаем текст кнопки
                                    btn_text = (await btn.inner_text()).strip()
                                    
                                    # Если текста нет - пробуем aria-label или value
                                    if not btn_text:
                                        btn_text = await btn.get_attribute("aria-label") or ""
                                        btn_text = btn_text.strip() if btn_text else ""
                                    
                                    # Если всё ещё нет текста - пробуем value для submit кнопок
                                    if not btn_text:
                                        btn_text = await btn.get_attribute("value") or ""
                                        btn_text = btn_text.strip() if btn_text else ""
                                    
                                    # Если совсем нет текста - пропускаем
                                    if not btn_text or len(btn_text) < 1:
                                        logger.debug(f"    [{idx}] ⚠️ Нет текста - пропускаем")
                                        continue
                                    
                                    button_count += 1
                                    
                                    # Логируем найденную кнопку
                                    logger.debug(f"    ✅ [{button_count}] {btn_text[:50]}")
                                    
                                    # Формируем hint с текстом кнопки
                                    hint_str = f'CLICK: strategy="text", args={{"text": "{btn_text[:60]}"}}'
                                    hints.append(f'  ➡️  {hint_str}')
                                    
                                except Exception as btn_error:
                                    logger.debug(f"    ⚠️ Ошибка обработки кнопки {idx}: {str(btn_error)[:40]}")
                            
                            if button_count == 0:
                                logger.debug(f"  ⚠️ Видимых кнопок в модали не найдено (всего элементов: {len(all_buttons)})")
                            else:
                                logger.debug(f"  ✅ Добавлено в hints: {button_count} видимых кнопок")
                        else:
                            logger.debug(f"  ⚠️ Кнопки в модали не найдены")
                            
                except Exception as e:
                    logger.debug(f"⚠️ Ошибка при поиске кнопок модали: {str(e)[:80]}")
            
            # ========== 7️⃣ Специальная обработка SEARCH INPUT ==========
            if not modal_window_open:  # ТОЛЬКО если нет модального окна
                try:
                    search_input = await self.page.get_by_placeholder("search").first.is_visible()
                    if search_input:
                        hints.append('There is a search input field (placeholder="search")')
                except:
                    pass
            
            # ========== 8️⃣ Если hints пусты - это может означать динамический контент ==========
            if not hints:
                hints.append('Page content looks dynamic or dialog appears. Try scrolling or waiting.')
            
            # ========== 📋 ЛОГИРОВАНИЕ: Показать все INPUT поля которые нашли ==========
            input_hints = [h for h in hints if "FILL:" in h or "INPUT FIELDS:" in h]
            if input_hints:
                logger.debug("✅ НАЙДЕННЫЕ INPUT ПОЛЯ:")
                for hint in input_hints:
                    logger.debug(f"   {hint}")
            else:
                logger.warning("⚠️  НЕ НАЙДЕНЫ INPUT ПОЛЯ на странице (это может быть проблемой!)")
            
            logger.debug(f"Поиск подсказок завершен: {len(hints)} элементов")
            return hints
            
        except Exception as e:
            logger.error(f"Ошибка при получении подсказок: {str(e)}")
            return ["Page analysis failed, check browser console"]
    
    async def _find_interactive_elements(self) -> List[InteractiveElement]:
        """
        ❌ DEPRECATED in v2: НЕ используется больше!
        Оставлена только для backward compatibility.
        
        v2 модель: 
        - PageAnalyzer возвращает HINTS, не элементы
        - ActionExecutor создаёт locator в момент действия
        - Playwright проверяет actionability
        """
        return []  # v2: empty, используем search_hints вместо этого

    async def _flatten_accessibility_tree(self, node: Dict[str, Any], depth: int = 0) -> List[Dict[str, Any]]:
        """
        ❌ УСТАРЕЛО: Больше не нужно, используем get_by_role() напрямую.
        Оставляем для совместимости, но не используем.
        """
        nodes = []
        if not node:
            return nodes
        if node.get('role'):
            nodes.append(node)
        if node.get('children'):
            for child in node['children']:
                nodes.extend(await self._flatten_accessibility_tree(child, depth + 1))
        return nodes

    def _map_accessibility_role_to_type(self, role: str) -> str:
        """
        Маппинг ARIA роли в тип элемента для InteractiveElement.
        """
        role_lower = role.lower()
        
        if role_lower in ['button', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'tab', 'treeitem']:
            return 'button'
        elif role_lower in ['link', 'doc-link']:
            return 'link'
        elif role_lower in ['textbox', 'searchbox']:
            return 'input'
        elif role_lower in ['checkbox']:
            return 'checkbox'
        elif role_lower in ['radio']:
            return 'radio'
        elif role_lower in ['combobox', 'listbox', 'select']:
            return 'select'
        elif role_lower in ['option']:
            return 'option'
        else:
            return role

    async def _get_headings(self) -> List[Dict[str, str]]:
        """Extract all headings from page"""
        try:
            headings = await self.page.evaluate("""
                () => {
                    return Array.from(document.querySelectorAll('h1, h2, h3, h4, h5, h6'))
                        .map(h => ({
                            level: h.tagName.toLowerCase(),
                            text: h.innerText.trim()
                        }))
                        .filter(h => h.text.length > 0);
                }
            """)
            return headings
        except:
            return []

    async def _identify_key_form_fields(self) -> List[Dict[str, Any]]:
        """Identify KEY form fields (не все, только главные) для LLM"""
        try:
            fields = []
            
            # Найти inputs используя Playwright get_by_role вместо CSS селектора
            textboxes = await self.page.get_by_role("textbox").all()
            searchboxes = await self.page.get_by_role("searchbox").all()
            all_inputs = textboxes + searchboxes
            
            for inp in all_inputs[:10]:  # Maximum 10 fields
                try:
                    # Получить label информацию используя evaluate (JavaScript)
                    label_info = await inp.evaluate("""
                        elem => {
                            let label_text = '';
                            
                            // Check for associated label via 'for' attribute
                            if (elem.id) {
                                let associated_label = document.querySelector(`label[for="${elem.id}"]`);
                                if (associated_label) {
                                    label_text = associated_label.innerText.trim();
                                }
                            }
                            
                            // Check for parent label
                            if (!label_text) {
                                let parent_label = elem.closest('label');
                                if (parent_label) {
                                    label_text = parent_label.innerText.trim();
                                }
                            }
                            
                            // Use aria-label
                            if (!label_text) {
                                label_text = elem.getAttribute('aria-label') || '';
                            }
                            
                            // Use placeholder as fallback
                            if (!label_text) {
                                label_text = elem.getAttribute('placeholder') || '';
                            }
                            
                            return {
                                label: label_text.substring(0, 50),
                                placeholder: elem.getAttribute('placeholder') || '',
                                id: elem.getAttribute('id') || ''
                            };
                        }
                    """)
                    
                    # Get input value using Playwright method
                    input_value = await inp.input_value()
                    
                    label_text = label_info.get('label', '')
                    if label_text:
                        fields.append({
                            "type": "input_field",
                            "label": label_text.strip()[:50],
                            "value": input_value or "",
                            "hint": f'Fill field "{label_text.strip()[:30]}"' + 
                                   (f' currently: "{input_value.strip()[:30]}"' if input_value else "")
                        })
                except:
                    pass
            
            return fields
        except:
            return []

    
    async def _detect_modals(self, analysis: PageAnalysis) -> None:
        """
        🚨 Detect REAL modal windows on the page (strict validation).
        
        Modal = overlay dialog that:
        1. Has role="dialog" OR role="alertdialog" (REQUIRED!)
        2. Blocks interaction with main content (has backdrop/overlay)
        3. Has visible buttons for interaction
        4. Covers significant portion of viewport
        
        ВАЖНО: НЕ считаем модальным окном:
        - Обычные уведомления которые просто видны на страице
        - Элементы с классом "modal" но без role="dialog"
        - Фоновые элементы без кнопок закрытия
        """
        try:
            # ========== МЕТОД 1: Поиск по ARIA role (САМЫЙ НАДЕЖНЫЙ) ==========
            # role="dialog" или role="alertdialog" - это явное указание что это модальное окно
            try:
                dialogs = await self.page.locator('[role="dialog"], [role="alertdialog"]').all()
                
                for dialog in dialogs:
                    try:
                        # Проверить видимость
                        is_visible = await dialog.is_visible()
                        if not is_visible:
                            continue
                        
                        # Проверить что диалог имеет достаточную высоту (реальное модальное окно)
                        bbox = await dialog.bounding_box()
                        if not bbox or bbox['height'] < 150:  # Слишком маленькое = не модаль
                            continue
                        
                        # Это реальное модальное окно!
                        logger.analysis("🚨 Обнаружено модальное окно (role='dialog')")
                        analysis.modal_open = True
                        
                        modal_text = await dialog.inner_text()
                        analysis.modal_text = modal_text
                        logger.analysis(f"📋 Текст модального окна: {analysis.modal_text[:100]}")
                        
                        # Попытаться найти стратегию закрытия
                        await self._find_modal_close_strategy(analysis, dialog)
                        return
                    except Exception as e:
                        logger.debug(f"  ⚠️ Ошибка при обработке диалога: {str(e)[:50]}")
                        continue
            except Exception as e:
                logger.debug(f"  ⚠️ Ошибка МЕТОДА 1: {str(e)[:50]}")
                pass
            
            # ========== МЕТОД 2: По CSS-селекторам (УНИВЕРСАЛЬНЫЙ ПУТЬ) ==========
            # Если сайт старый или не следует стандартам доступности
            # Ищем по характерным классам: modal, popup, dialog
            # Проверяем видимость и что это действительно открыто
            try:
                logger.debug("🔍 МЕТОД 2: Поиск по CSS-селекторам (modal/popup/dialog)...")
                
                # Селектор перебирает частые названия классов и атрибутов
                modal_selector = 'div[class*="modal"], div[class*="popup"], [role="dialog"], .fade.show'
                modal_locator = self.page.locator(modal_selector)
                count = await modal_locator.count()
                
                if count > 0:
                    logger.debug(f"  ✅ Найдено {count} потенциальных модальных окон по CSS селектору")
                    # Берем ПОСЛЕДНЕЕ окно (обычно оно поверх всех)
                    modal_elem = modal_locator.last
                    
                    # Проверяем что это действительно видимое и открытое окно
                    try:
                        is_visible = await modal_elem.is_visible()
                        if not is_visible:
                            logger.debug("  ⚠️ Элемент не видим по is_visible() - пропускаем")
                            raise Exception("Not visible")
                        
                        # Проверяем размер
                        bbox = await modal_elem.bounding_box()
                        if not bbox or bbox['height'] < 150:
                            logger.debug(f"  ⚠️ Элемент слишком маленький ({bbox['height'] if bbox else 0} px) - пропускаем")
                            raise Exception("Too small")
                        
                        # Это реальное модальное окно!
                        logger.analysis("🚨 Обнаружено модальное окно по CSS классам")
                        analysis.modal_open = True
                        
                        modal_text = await modal_elem.inner_text()
                        analysis.modal_text = modal_text
                        logger.analysis(f"📋 Текст модального окна: {analysis.modal_text[:100]}")
                        
                        # Попытаться найти стратегию закрытия
                        await self._find_modal_close_strategy(analysis, modal_elem)
                        return
                    except Exception as e:
                        logger.debug(f"  ⚠️ Ошибка при обработке CSS элемента: {str(e)[:50]}")
                        pass
                else:
                    logger.debug(f"  ℹ️ Модальные окна по CSS селектору не найдены")
            except Exception as e:
                logger.debug(f"  ⚠️ Ошибка МЕТОДА 2: {str(e)[:50]}")
                pass
            
            # ========== ФИНАЛ: Модального окна не найдено ==========
            # Если оба метода не сработали - модального окна нет
            analysis.modal_open = False
            logger.debug("✓ Видимое модальное окно не обнаружено")
        
        except Exception as e:
            logger.debug(f"Ошибка при обнаружении модальных окон: {e}")
            analysis.modal_open = False

    async def _find_modal_close_strategy(self, analysis: PageAnalysis, modal_locator) -> None:
        """
        🔍 Найти кнопку или механизм для закрытия модального окна.
        
        ВАЖНО: Ищем элементы ТОЛЬКО ВНУТРИ видимой модали!
        Используем modal_locator.get_by_role() вместо page.get_by_role()
        чтобы найти элементы исключительно внутри этой модали.
        
        Приоритет:
        1. Кнопка "Close" с иконкой X или текстом "close"
        2. Кнопки Cancel/No/OK
        3. ESC ключ
        4. Клик вне модали
        """
        try:
            # ========== СТРАТЕГИЯ 1: Ищем кнопку "Close" / X ==========
            # Ищем по aria-label или текстом
            # ВАЖНО: Используем modal_locator.get_by_role() чтобы искать ТОЛЬКО внутри модали!
            try:
                logger.debug("  🔍 Ищем кнопку закрытия (X или 'Close')...")
                close_buttons = await modal_locator.get_by_role("button").all()
                
                for btn in close_buttons:
                    try:
                        button_text = (await btn.inner_text()).strip()
                        aria_label = await btn.get_attribute("aria-label")
                        
                        # Проверяем текст и aria-label на наличие "close"
                        is_close_button = (
                            button_text.lower() in ["close", "x", "✕", "×"] or
                            (aria_label and ("close" in aria_label.lower() or "закрыть" in aria_label.lower()))
                        )
                        
                        if is_close_button:
                            logger.analysis(f"✅ Найдена кнопка закрытия: '{button_text or aria_label}'")
                            close_element = InteractiveElement(
                                element_id="modal_close",
                                element_type="button",
                                text=button_text or (aria_label or "Close"),
                                selector="[role='button']",
                                description="Modal close button"
                            )
                            close_element.locator_strategy = "text"
                            close_element.locator_args = {"text": button_text or aria_label}
                            close_element.can_click = True
                            analysis.modal_close_element = close_element
                            return
                    except Exception as e:
                        logger.debug(f"    ⚠️ Ошибка при проверке кнопки: {str(e)[:40]}")
                        pass
            except Exception as e:
                logger.debug(f"  ⚠️ Ошибка при поиске кнопок Close: {str(e)[:50]}")
                pass
            
            # ========== СТРАТЕГИЯ 2: Кнопки Cancel/No/Отмена ==========
            # Ищем кнопки с типичными текстами закрытия/отмены
            try:
                logger.debug("  🔍 Ищем кнопку Cancel/Отмена/No...")
                action_button_texts = [
                    "Cancel", "cancel", "CANCEL",
                    "No", "no", "NO",
                    "Отмена", "отмена",
                    "Закрыть", "закрыть",
                    "Нет", "нет"
                ]
                
                buttons = await modal_locator.get_by_role("button").all()
                for btn in buttons:
                    btn_text = (await btn.inner_text()).strip()
                    if btn_text in action_button_texts:
                        logger.analysis(f"✅ Найдена кнопка действия: '{btn_text}'")
                        close_element = InteractiveElement(
                            element_id="modal_close",
                            element_type="button",
                            text=btn_text,
                            selector="button",
                            description=f"Modal action button: {btn_text}"
                        )
                        close_element.locator_strategy = "text"
                        close_element.locator_args = {"text": btn_text}
                        close_element.can_click = True
                        analysis.modal_close_element = close_element
                        return
            except Exception as e:
                logger.debug(f"  ⚠️ Ошибка при поиске кнопок Cancel: {str(e)[:50]}")
                pass
            
            # ========== СТРАТЕГИЯ 3: Если есть кнопки - берем ПЕРВУЮ ==========
            # ВАЖНО: Используем modal_locator.get_by_role() чтобы искать ТОЛЬКО внутри модали
            try:
                logger.debug("  🔍 Будет использована ПЕРВАЯ кнопка в модали...")
                buttons = await modal_locator.get_by_role("button").all()
                if buttons:
                    first_btn_text = (await buttons[0].inner_text()).strip()
                    if first_btn_text:
                        logger.analysis(f"✅ Будет использована первая кнопка: '{first_btn_text[:30]}'")
                        close_element = InteractiveElement(
                            element_id="modal_close",
                            element_type="button",
                            text=first_btn_text,
                            selector="button:first-of-type",
                            description="First modal button"
                        )
                        close_element.locator_strategy = "text"
                        close_element.locator_args = {"text": first_btn_text}
                        close_element.can_click = True
                        analysis.modal_close_element = close_element
                        return
            except Exception as e:
                logger.debug(f"  ⚠️ Ошибка при поиске первой кнопки: {str(e)[:50]}")
                pass
            
            # ========== СТРАТЕГИЯ 4: ESC ключ как fallback ==========
            logger.analysis("⚠️ Не найдена кнопка закрытия, будет использован ESC ключ")
            close_element = InteractiveElement(
                element_id="modal_close_esc",
                element_type="key_press",
                text="ESC",
                selector="",
                description="Press ESC to close modal"
            )
            close_element.can_click = True  # Mark as "actionable" even though it's a key press
            analysis.modal_close_element = close_element
            
        except Exception as e:
            logger.debug(f"Ошибка при поиске стратегии закрытия модали: {e}")
    
    async def _log_page_stats(self, analysis: PageAnalysis):
        """
        Логировать статистику страницы: размер, количество элементов, текста и т.д.
        """
        try:
            # Get page size via JavaScript
            page_size = await self.page.evaluate("""
                () => {
                    // Approximate page size by counting DOM nodes and content
                    const html = document.documentElement.outerHTML;
                    return {
                        html_bytes: new Blob([html]).size,
                        elements_count: document.querySelectorAll('*').length,
                    };
                }
            """)
            
            # Calculate content size
            text_size = len(analysis.main_text.encode('utf-8'))
            hints_size = len(str(analysis.search_hints).encode('utf-8'))
            
            total_collected = text_size + hints_size
            html_mb = page_size['html_bytes'] / (1024 * 1024)
            collected_mb = total_collected / (1024 * 1024)
            
            logger.dom(f"Размер HTML: {html_mb:.2f} МБ")
            logger.dom(f"Информация: {collected_mb:.2f} МБ (текст: {text_size / 1024:.1f} КБ + hints: {hints_size / 1024:.1f} КБ)")
            logger.dom(f"Элементов: {page_size['elements_count']} | Подсказок: {len(analysis.search_hints)}")
            
            # Log video error if detected
            if analysis.video_error:
                logger.warning(f"🎥 VIDEO ERROR DETECTED: {analysis.video_error}")
            
        except Exception as e:
            logger.debug(f"Ошибка при сборе статистики: {e}")