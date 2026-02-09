"""
Action executor module.
Executes browser actions based on task requirements.
"""
from typing import Optional, Dict, Any
from playwright.async_api import Page, Locator
from logger import logger
from disambiguation_layer import DisambiguationLayer
import asyncio


class ActionExecutor:
    """Executes browser actions"""

    def __init__(self, page: Page):
        self.page = page
        self.disambiguation = DisambiguationLayer(page)

    def _build_locator_from_strategy(self, strategy: str, args: Dict[str, Any]) -> Locator:
        """
        🎯 Build a Playwright locator from strategy and args.
        
        ПРИОРИТЕТ СТРАТЕГИЙ (от самых стабильных к техническим):
        1. role (ARIA-роль + имя) - самое стабильное
        2. label (aria-label) - для меток формы
        3. placeholder - для input полей
        4. text (видимый текст) - для ссылок и кнопок
        5. alt (альт-текст) - для изображений
        6. title - для всплывающих подсказок
        7. testid (data-testid) - специальный атрибут для тестирования
        8. id - технический атрибут
        9. name - технический атрибут для форм
        10. data-* (другие data атрибуты) - кастомные атрибуты
        
        Args:
            strategy: Одна из перечисленных выше стратегий
            args: Strategy-specific arguments
            
        Returns:
            Playwright Locator object
        """
        # 1️⃣ ROLE + NAME (ARIA-роль и доступное имя)
        if strategy == "role":
            role = args.get("role", "button")
            name = args.get("name")
            if name:
                return self.page.get_by_role(role, name=name)
            else:
                return self.page.get_by_role(role)
        
        # 2️⃣ LABEL (aria-label или label text)
        elif strategy == "label":
            label_text = args.get("label", "")
            if label_text:
                return self.page.get_by_label(label_text)
            else:
                logger.warning(f"strategy='label' but label is empty in args: {args}")
                return self.page.locator(":invalid")
        
        # 3️⃣ PLACEHOLDER (для input полей)
        elif strategy == "placeholder":
            placeholder = args.get("placeholder", "")
            if placeholder:
                return self.page.get_by_placeholder(placeholder)
            else:
                logger.warning(f"strategy='placeholder' but placeholder is empty")
                # Fallback: используем get_by_role вместо locator("input")
                return self.page.get_by_role("textbox").first
        
        # 4️⃣ TEXT (видимый текст)
        elif strategy == "text":
            text = args.get("text", "")
            if text:
                is_link = args.get("is_link", False)
                link_context = args.get("context", "")
                
                if is_link:
                    # Для ссылок используем get_by_role("link") с фильтром по тексту
                    base_locator = self.page.get_by_role("link").filter(has_text=text)
                    
                    # Если есть контекст (YouTube, VK и т.д.), попробовать найти более точное совпадение
                    if link_context:
                        filtered = self.page.get_by_role("link").filter(has_text=link_context).filter(has_text=text)
                        return filtered.first
                    
                    return base_locator.first
                else:
                    # Для обычного текста: используем partial matching
                    return self.page.get_by_text(text, exact=False).first
            else:
                logger.warning(f"strategy='text' but text is empty in args: {args}")
                return self.page.locator(":invalid")
        
        # 5️⃣ ALT-TEXT (для изображений)
        elif strategy == "alt":
            alt_text = args.get("alt", "")
            if alt_text:
                return self.page.get_by_alt_text(alt_text)
            else:
                logger.warning(f"strategy='alt' but alt is empty in args: {args}")
                return self.page.locator(":invalid")
        
        # 6️⃣ TITLE (всплывающие подсказки)
        elif strategy == "title":
            title = args.get("title", "")
            if title:
                return self.page.get_by_title(title)
            else:
                logger.warning(f"strategy='title' but title is empty in args: {args}")
                return self.page.locator(":invalid")
        
        # 7️⃣ TESTID (data-testid или другой атрибут - по умолчанию data-testid)
        elif strategy == "testid":
            testid = args.get("testid", "")
            if testid:
                return self.page.get_by_test_id(testid)
            else:
                logger.warning(f"strategy='testid' but testid is empty in args: {args}")
                return self.page.locator(":invalid")
        
        # 8️⃣ ID (CSS id selector или атрибут селектор)
        elif strategy == "id":
            element_id = args.get("id", "")
            if element_id:
                # ⚠️  ВАЖНО: Используем атрибут селектор [id="..."] вместо #id
                # потому что ID может содержать специальные символы (двоеточие, точка и т.д.)
                # которые имеют специальное значение в CSS селекторах и нужны бы экранирования.
                # Атрибут селектор работает с любыми символами в значении.
                return self.page.locator(f'[id="{element_id}"]')
            else:
                logger.warning(f"strategy='id' but id is empty in args: {args}")
                return self.page.locator(":invalid")
        
        # 9️⃣ NAME (HTML name attribute)
        elif strategy == "name":
            name = args.get("name", "")
            if name:
                return self.page.locator(f'[name="{name}"]')
            else:
                logger.warning(f"strategy='name' but name is empty in args: {args}")
                return self.page.locator(":invalid")
        
        # 🔟 DATA-* ATTRIBUTES (кастомные data атрибуты)
        elif strategy.startswith("data-"):
            # strategy like "data-city", "data-testid", "data-value"
            attr_value = args.get(strategy, "")
            if attr_value:
                return self.page.locator(f'[{strategy}="{attr_value}"]')
            else:
                logger.warning(f"strategy='{strategy}' but value is empty in args: {args}")
                return self.page.locator(":invalid")
        
        # aria-label (как альтернатива для label)
        elif strategy == "aria-label":
            aria_label = args.get("aria-label", "")
            if aria_label:
                return self.page.get_by_label(aria_label)
            else:
                logger.warning(f"strategy='aria-label' but aria-label is empty in args: {args}")
                return self.page.locator(":invalid")
        
        else:
            # Fallback to CSS selector or unknown strategy
            logger.error(f"Unknown strategy '{strategy}' - cannot build locator")
            return self.page.locator(":invalid")  # Return invalid locator that will fail cleanly

    async def click(self, locator_strategy: str = None, locator_args: Dict[str, Any] = None,
                   button: str = "left", click_count: int = 1, element_text: str = "", 
                   allow_multiple: bool = False) -> Dict[str, Any]:
        """
        v2: Клик на элемент с strict_mode handling
        
        Процесс:
        1. Создать Playwright locator из strategy
        2. Проверить locator.count() - есть ли элементы
        3. Если 1: выполнить click
        4. Если 0: вернуть error
        5. Если >1 и NOT allow_multiple: вернуть strict_violation с вариантами
        
        Playwright автоматически проверит actionability:
        ✓ видимость элемента
        ✓ стабильность в DOM
        ✓ может ли элемент получить события
        ✓ enabled/disabled
        
        Args:
            locator_strategy: Strategy для поиска ("role", "text", "placeholder", "css")
            locator_args: Arguments для strategy
            button: Кнопка мыши ('left', 'right', 'middle')
            click_count: Количество кликов
            element_text: Текст элемента для логирования
            allow_multiple: Если True, клик на первый элемент (иначе error при multiple matches)
            
        Returns:
            Dict со статусом:
            - {"success": true} если OK
            - {"error": "strict_mode_violation", "count": N, "variants": [...]} если >1 элемент
            - {"error": "not_found"} если 0 элементов
            - {"error": "actionability", "reason": "..."} если Playwright выбросил исключение
        """
        try:
            if not locator_strategy or not locator_args:
                logger.error("Нет данных для клика (strategy/args пусты)")
                return {"error": "invalid_params", "reason": "strategy or args empty"}
            
            # 1️⃣ Build locator
            locator = self._build_locator_from_strategy(locator_strategy, locator_args)
            
            # 2️⃣ Check how many elements match
            count = await locator.count()
            
            if count == 0:
                logger.error(f"Элемент не найден: {element_text or locator_strategy}")
                return {"error": "element_not_found"}
            
            elif count > 1:
                if not allow_multiple:
                    # Найдено больше одного элемента - собрираем информацию о вариантах
                    logger.warning(f"Найдено {count} элементов (ambiguous): {element_text or locator_strategy}")
                    
                    # Для ссылок - получи подробнее информацию о каждой
                    variants = []
                    strategy = locator_strategy or "text"
                    is_link = strategy == "text" and (locator_args or {}).get("is_link", False)
                    
                    if is_link and count <= 5:  # Only for small number of links
                        try:
                            all_links = await locator.all()
                            for i, link in enumerate(all_links[:5]):  # Max 5 variants
                                try:
                                    link_text = await link.text_content()
                                    link_href = await link.get_attribute("href")
                                    
                                    # Extract domain name for context
                                    domain = "unknown"
                                    if link_href:
                                        import urllib.parse
                                        parsed = urllib.parse.urlparse(link_href)
                                        domain = parsed.netloc or parsed.scheme
                                    
                                    first_line = (link_text or "").split('\n')[0].strip()[:50] if link_text else "untitled"
                                    
                                    variants.append({
                                        "index": i,
                                        "text": first_line,
                                        "domain": domain,
                                        "href": link_href[:60] if link_href else ""
                                    })
                                except:
                                    variants.append({"index": i, "text": "Link (could not parse)"})
                        except:
                            pass
                    
                    # Вернём варианты для LLM или для уточнения
                    return {
                        "error": "multiple_matches",
                        "count": count,
                        "suggestion": f"Найдено {count} совпадений. Время LLM выбрать более конкретный вариант используя контекст результата поиска.",
                        "variants": variants if variants else [{"text": "Multiple matches", "index": i} for i in range(min(count, 3))],
                        "reason": f"Модель предоставила неточный селектор - найдено {count} элементов вместо 1. Переформулируй условие для клика (добавь платформу, автора, источник и т.д.)"
                    }
            
            # Single element - execute click
            logger.action(f"Кликаю на: {element_text or locator_strategy}")
            
            # Playwright проверит actionability и выбросит исключение если не OK
            click_locator = locator
            
            # 🔍 Отслеживаем сколько страниц открыто перед кликом
            pages_before = len(self.page.context.pages)
            
            # Выполняем клик (может открыть новую страницу)
            await click_locator.click(button=button, click_count=click_count, timeout=5000, force=False)
            
            # Проверим открылась ли новая страница
            await asyncio.sleep(0.5)
            pages_after = len(self.page.context.pages)
            
            if pages_after > pages_before:
                # Открылась новая страница/вкладка!
                logger.warning(f"Клик открыл новую вкладку! Было {pages_before}, стало {pages_after}")
                all_pages = self.page.context.pages
                new_page = all_pages[-1]  # Последняя открытая страница
                
                # Подождем загрузки новой страницы
                try:
                    await new_page.wait_for_load_state("domcontentloaded", timeout=10000)
                    logger.info(f"Переключаюсь на новую вкладку")
                    self.page = new_page
                    self.disambiguation.page = new_page  # Обновить и в disambiguation слое
                    
                    # Закроем лишние вкладки кроме текущей
                    for p in all_pages:
                        if p != self.page and p != all_pages[0]:  # Оставим одну резервную
                            try:
                                await p.close()
                                logger.debug(f"Закрыл лишнюю вкладку")
                            except:
                                pass
                except Exception as wait_error:
                    logger.warning(f"Ошибка загрузки новой страницы: {wait_error}")
            
            logger.success(f"Клик выполнен")
            return {"success": True}
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Ошибка клика: {error_msg[:100]}")
            
            # Categorize error for better LLM understanding
            if "not visible" in error_msg or "not in viewport" in error_msg:
                reason = "Element not visible to user (off-screen or hidden)"
            elif "disabled" in error_msg or "not enabled" in error_msg:
                reason = "Element is disabled"
            elif "not stable" in error_msg:
                reason = "Element not stable in DOM (moving or detached)"
            elif "no element matches" in error_msg or "no such element" in error_msg:
                reason = "Element not found in DOM"
            elif "pointer-events" in error_msg:
                reason = "Element cannot receive events (pointer-events)"
            elif "hidden behind" in error_msg or "covered" in error_msg:
                reason = "Element covered by another element"
            else:
                reason = error_msg[:100]
            
            return {"error": "actionability", "reason": reason}


    async def fill(self, locator_strategy: str = None, locator_args: Dict[str, Any] = None,
                  text: str = "", element_text: str = "") -> Dict[str, Any]:
        """
        v2: Заполнить поле ввода с strict_mode handling
        
        Процесс аналогичен click():
        1. Создать locator
        2. Проверить count()
        3. Если >1: вернуть strict_violation
        4. Если 1: fill
        
        Playwright проверит:
        ✓ видимость
        ✓ enabled
        ✓ может быть заполнен (input/textarea/contenteditable)
        
        Args:
            locator_strategy: Strategy для поиска
            locator_args: Arguments для strategy
            text: Текст для заполнения
            element_text: Текст элемента для логирования
            
        Returns:
            Dict со статусом (аналогично click)
        """
        try:
            if not locator_strategy or not locator_args:
                logger.error("Нет данных для заполнения (strategy/args пусты)")
                return {"error": "invalid_params"}
            
            locator = self._build_locator_from_strategy(locator_strategy, locator_args)
            
            # Check element count
            count = await locator.count()
            
            if count == 0:
                logger.error(f"Поле не найдено: {element_text or locator_strategy}")
                return {"error": "element_not_found"}
            
            elif count > 1:
                logger.warning(f"STRICT MODE: Найдено {count} полей вместо 1")
                
                # Если найдено СЛИШКОМ МНОГО элементов - не пытаемся их все перебирать
                if count > 50:
                    logger.error(f"Стратегия слишком общая: найдено {count} элементов")
                    return {
                        "error": "strategy_too_generic",
                        "count": count,
                        "message": f"Стратегия нашла {count} элементов - нужна более специфичная стратегия (addFilter, использовать aria-label, ID, название, кнопка рядом и т.д.)"
                    }
                
                variants = []
                all_locators = await locator.all()
                for i, loc in enumerate(all_locators[:5]):
                    try:
                        placeholder = await loc.get_attribute("placeholder")
                        label_text = "unknown"
                        try:
                            loc_id = await loc.get_attribute("id")
                            if loc_id:
                                # Используем evaluate вместо .locator() для поиска связанного label
                                label_el = await self.page.evaluate(f"""
                                    () => {{
                                        let l = document.querySelector('label[for="{loc_id}"]');
                                        return l ? l.innerText.trim() : '';
                                    }}
                                """)
                                if label_el:
                                    label_text = label_el[:30]
                                else:
                                    label_text = placeholder[:30] if placeholder else f"Field {i}"
                            else:
                                label_text = placeholder[:30] if placeholder else f"Field {i}"
                        except:
                            label_text = placeholder[:30] if placeholder else f"Field {i}"
                        
                        variants.append({
                            "index": i,
                            "label": label_text
                        })
                    except:
                        variants.append({"index": i, "label": f"Field {i}"})
                
                return {
                    "error": "strict_mode_violation",
                    "count": count,
                    "variants": variants
                }
            
            # Execute fill
            logger.action(f"Заполняю: {element_text or locator_strategy} = '{text[:50]}'")
            
            fill_locator = locator.first if count > 1 else locator
            await fill_locator.fill(text, timeout=5000)
            
            await asyncio.sleep(0.3)
            logger.success(f"Заполнение завершено")
            return {"success": True}
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Ошибка заполнения: {error_msg[:100]}")
            
            if "not visible" in error_msg or "not in viewport" in error_msg:
                reason = "Field not visible"
            elif "disabled" in error_msg or "readonly" in error_msg:
                reason = "Field is disabled or readonly"
            elif "not supported" in error_msg or "not editable" in error_msg:
                reason = "Field cannot be filled (not input/textarea)"
            elif "no element matches" in error_msg:
                reason = "Field not found"
            else:
                reason = error_msg[:100]
            
            return {"error": "actionability", "reason": reason}


    async def type_text(self, locator_strategy: str = None, locator_args: Dict[str, Any] = None,
                       text: str = "", delay: int = 0, element_text: str = "") -> Dict[str, Any]:
        """
        v2: Ввести текст посимвольно с strict_mode handling
        
        Используется когда нужен отдельный ввод для каждого символа
        (например, для автозаполнения или специальных обработчиков).
        
        Playwright проверит:
        ✓ видимость
        ✓ enabled
        ✓ может принять фокус
        ✓ может получить события
        
        Args:
            locator_strategy: Strategy для поиска
            locator_args: Arguments для strategy
            text: Текст для ввода
            delay: Задержка между символами в мс
            element_text: Текст элемента для логирования
            
        Returns:
            Dict со статусом
        """
        try:
            if not locator_strategy or not locator_args:
                logger.error("Нет данных для ввода (strategy/args пусты)")
                return {"error": "invalid_params"}
            
            locator = self._build_locator_from_strategy(locator_strategy, locator_args)
            
            # Check element count
            count = await locator.count()
            
            if count == 0:
                logger.error(f"Поле не найдено: {element_text}")
                return {"error": "element_not_found"}
            
            elif count > 1:
                logger.warning(f"STRICT MODE: Найдено {count} полей вместо 1")
                return {"error": "strict_mode_violation", "count": count}
            
            # Execute type
            logger.action(f"Ввожу в: {element_text or locator_strategy}")
            
            type_locator = locator.first if count > 1 else locator
            
            if delay > 0:
                # Posymbol input with delay
                for char in text:
                    await type_locator.type(char, delay=delay, timeout=5000)
            else:
                # Normal input
                await type_locator.type(text, timeout=5000)
            
            await asyncio.sleep(0.3)
            logger.success(f"Ввод завершен")
            return {"success": True}
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Ошибка ввода: {error_msg[:100]}")
            
            if "not visible" in error_msg or "not in viewport" in error_msg:
                reason = "Element not visible"
            elif "disabled" in error_msg or "not enabled" in error_msg:
                reason = "Element is disabled"
            elif "no element matches" in error_msg:
                reason = "Element not found"
            elif "not editable" in error_msg or "not supported" in error_msg:
                reason = "Element cannot be filled (not input/textarea)"
            else:
                reason = error_msg[:100]
            
            return {"error": "actionability", "reason": reason}


    async def goto(self, url: str) -> bool:
        """
        Перейти на URL
        
        Args:
            url: URL для переходя
            
        Returns:
            True если успешно
        """
        logger.navigation(f"Переходу на: {url}")
        try:
            # Use domcontentloaded for faster page loads on heavy JS sites
            # networkidle is too strict and causes timeouts on sites with heavy JavaScript
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            logger.success("Навигация выполнена")
            return True
        except Exception as e:
            logger.error(f"Навигация не выполнена: {str(e)}")
            return False

    async def scroll(self, direction: str = "down", amount: int = 3) -> bool:
        """
        Прокрутить страницу
        
        Args:
            direction: 'up' или 'down'
            amount: Количество прокруток
            
        Returns:
            True если успешно
        """
        logger.action(f"Прокручиваю {direction}", "ПРОКРУТКА")
        try:
            if direction.lower() == "down":
                for _ in range(amount):
                    await self.page.keyboard.press("PageDown")
                    await asyncio.sleep(0.2)
            elif direction.lower() == "up":
                for _ in range(amount):
                    await self.page.keyboard.press("PageUp")
                    await asyncio.sleep(0.2)
            
            await asyncio.sleep(0.5)
            logger.success("Прокрутка выполнена")
            return True
        except Exception as e:
            logger.error(f"Прокрутка не выполнена: {str(e)}")
            return False

    async def press_key(self, key: str, locator_strategy: str = None, locator_args: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Нажать клавишу (Enter, Escape, Tab и т.д.)
        
        Если указан locator - нажимает в контексте этого элемента (фокус на элемент)
        Если locator не указан - нажимает на текущей странице
        
        Args:
            key: "Enter", "Escape", "Tab", "ArrowUp", "ArrowDown", " " (space), etc.
            locator_strategy: Optional, strategy для поиска элемента
            locator_args: Optional, arguments для strategy
            
        Returns:
            Dict со статусом
        """
        try:
            if locator_strategy and locator_args:
                # Нажать в контексте элемента (сначала дать ему фокус)
                locator = self._build_locator_from_strategy(locator_strategy, locator_args)
                count = await locator.count()
                
                if count == 0:
                    logger.error(f"Элемент не найден для нажатия клавиши")
                    return {"error": "element_not_found"}
                
                target_locator = locator.first if count > 1 else locator
                logger.action(f"Нажимаю {key} на элементе")
                await target_locator.press(key)
            else:
                # Нажать на странице
                logger.action(f"Нажимаю {key} на странице")
                await self.page.press("body", key)
            
            logger.success(f"Клавиша {key} нажата")
            return {"success": True}
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Ошибка нажатия клавиши {key}: {error_msg[:100]}")
            return {"error": "key_press_failed", "reason": error_msg[:100]}

    async def wait_for_user_action(self, reason: str = "") -> bool:
        """
        Ждать пока пользователь пройдт КАПЧА, 2FA или другую человеческую проверку.
        Agent паузируется и ждет пока пользователь скажет что готово.
        
        Args:
            reason: Пояснение что нужно сделать пользователю
            
        Returns:
            True после того как пользователь подтвердит
        """
        logger.warning(f"ОЖИДАНИЕ ДЕЙСТВИЯ ПОЛЬЗОВАТЕЛЯ")
        logger.warning(f"📌 {reason or 'Пожалуйста выполните требуемое действие вручную'}")
        logger.warning(f"📌 После завершения, нажмите Enter чтобы продолжить")
        
        try:
            # Wait for user to press Enter
            input("\n➡️  Нажмите Enter когда готово: ")
            logger.success("Пользователь подтвердил, продолжаю")
            return True
        except KeyboardInterrupt:
            logger.error("Отменено пользователем")
            return False
        except Exception as e:
            logger.error(f"Ошибка при ожидании: {str(e)}")
            return False
    async def close_modal(self, close_strategy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        🚨 Закрыть модальное окно.
        
        Стратегии (в порядке приоритета):
        1. Клик на кнопку закрытия (если указана)
        2. Нажатие ESC
        3. Клик вне модального окна
        
        Args:
            close_strategy: Dict с информацией о кнопке закрытия:
                {
                    "type": "button" | "esc" | "outside",
                    "strategy": "text" | "role" | "css",  # for buttons
                    "args": {...}  # for buttons
                }
        
        Returns:
            Dict с результатом: {"success": true} или {"error": "..."}
        """
        try:
            if not close_strategy:
                # Стратегия по умолчанию: попробуем ESC
                logger.info("🚨 Закрываю модальное окно нажатием ESC")
                return await self.key_press("Escape")
            
            strategy_type = close_strategy.get("type", "button")
            
            if strategy_type == "button":
                # Клик на кнопку закрытия
                strategy = close_strategy.get("strategy")
                args = close_strategy.get("args", {})
                
                if strategy and args:
                    logger.info("🚨 Закрываю модальное окно кликом на кнопку")
                    result = await self.click(
                        locator_strategy=strategy,
                        locator_args=args,
                        element_text=args.get("text", "close button")
                    )
                    
                    if result.get("success"):
                        logger.success("✅ Модальное окно закрыто")
                        # Wait a bit for modal to close
                        await asyncio.sleep(0.5)
                    
                    return result
                else:
                    # No strategy for button, fall back to ESC
                    logger.info("🚨 Нет стратегии для кнопки, используюю ESC")
                    return await self.key_press("Escape")
            
            elif strategy_type == "esc":
                logger.info("🚨 Закрываю модальное окно нажатием ESC")
                result = await self.key_press("Escape")
                
                if result.get("success"):
                    logger.success("✅ Модальное окно закрыто ESC")
                    await asyncio.sleep(0.5)
                
                return result
            
            elif strategy_type == "outside":
                # Клик вне модального окна
                logger.info("🚨 Закрываю модальное окно кликом вне окна")
                try:
                    # Click on top-left corner (usually safe spot outside modal)
                    await self.page.click("body", position={"x": 10, "y": 10})
                    logger.success("✅ Модальное окно закрыто")
                    await asyncio.sleep(0.5)
                    return {"success": True}
                except Exception as e:
                    logger.error(f"Ошибка клика вне модального окна: {str(e)}")
                    return {"error": "outside_click_failed"}
            
            else:
                logger.error(f"Неизвестный тип закрытия модального окна: {strategy_type}")
                return {"error": "unknown_close_strategy"}
        
        except Exception as e:
            logger.error(f"Ошибка при закрытии модального окна: {str(e)}")
            return {"error": "modal_close_failed", "reason": str(e)[:100]}
    async def wait_for_timeout(self, ms: int = 1000) -> bool:
        """
        Ждать N миллисекунд
        
        Args:
            ms: Миллисекунды
            
        Returns:
            True
        """
        logger.action(f"Жду {ms}ms", "ОЖИДАНИЕ")
        try:
            await asyncio.sleep(ms / 1000)
            logger.success(f"Прошло {ms}ms")
            return True
        except Exception as e:
            logger.error(f"Ошибка ожидания: {str(e)}")
            return False