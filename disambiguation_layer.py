"""
Disambiguation Layer для Progressive Narrowing.

Вместо попыток LLM выбрать уникальный элемент с первой попытки,
этот слой применяет smart narrowing правила когда найдено несколько совпадений.

Правила narrowing (в порядке приоритета):
1. Scope narrowing - ограничить область поиска (main, form и т.д.)
2. Visibility narrowing - только видимые элементы
3. Position narrowing - выбрать первый/последний/nth
4. Proximity narrowing - элементы рядом с другими
5. Ask user - если всё ещё неоднозначно
"""
from typing import Dict, Any, List, Optional
from playwright.async_api import Locator, Page
from logger import logger
import asyncio
import re


class DisambiguationLayer:
    """Smart narrowing для разрешения ambiguous locators"""
    
    def __init__(self, page: Page):
        self.page = page
        self.narrowing_log = []
    
    async def resolve_ambiguous_locator(
        self,
        locator: Locator,
        initial_count: int,
        intent: str = "",
        strategy: str = "",
        args: Dict = None
    ) -> Dict[str, Any]:
        """
        Разрешить неоднозначный locator используя progressive narrowing.
        
        Args:
            locator: Найденный Playwright locator
            initial_count: Сколько элементов найдено
            intent: Что пытаемся сделать (например "фильтр размера")
            strategy: Стратегия поиска (role, label, text и т.д.)
            args: Аргументы стратегии
            
        Returns:
            {
                "success": True/False,
                "locator": Locator (если успешно разрешено),
                "final_count": int,
                "narrowing_steps": List[str]
            }
        """
        self.narrowing_log = []
        
        logger.info(f"🔍 NARROWING: Начинаю разрешение {initial_count} найденных элементов")
        logger.info(f"   intent: {intent}")
        logger.info(f"   strategy: {strategy}")
        
        current_locator = locator
        current_count = initial_count
        
        # Шаг 1: SCOPE NARROWING - ограничить область (main, form, модальное окно)
        scope_result = await self._apply_scope_narrowing(current_locator, intent)
        if scope_result:
            current_locator = scope_result["locator"]
            current_count = scope_result["count"]
            self.narrowing_log.append(scope_result["reason"])
            logger.info(f"  🔽 Scope narrowing: {scope_result['reason']}")
        
        if current_count == 1:
            return {
                "success": True,
                "locator": current_locator,
                "final_count": 1,
                "narrowing_steps": self.narrowing_log
            }
        
        # Шаг 2: VISIBILITY NARROWING - только видимые элементы
        visibility_result = await self._apply_visibility_narrowing(current_locator)
        if visibility_result:
            current_locator = visibility_result["locator"]
            current_count = visibility_result["count"]
            self.narrowing_log.append(visibility_result["reason"])
            logger.info(f"  🔽 Visibility narrowing: {visibility_result['reason']}")
        
        if current_count == 1:
            return {
                "success": True,
                "locator": current_locator,
                "final_count": 1,
                "narrowing_steps": self.narrowing_log
            }
        
        # Шаг 3: POSITION NARROWING - выбрать первый видимый/внутри viewport
        position_result = await self._apply_position_narrowing(
            current_locator,
            intent
        )
        if position_result:
            current_locator = position_result["locator"]
            current_count = position_result["count"]
            self.narrowing_log.append(position_result["reason"])
            logger.info(f"  🔽 Position narrowing: {position_result['reason']}")
        
        if current_count == 1:
            return {
                "success": True,
                "locator": current_locator,
                "final_count": 1,
                "narrowing_steps": self.narrowing_log
            }
        
        # Шаг 4: PROXIMITY NARROWING - если есть контекст (например "товар в списке")
        # Это более специфичное, пропускаем для теперь
        
        # Если всё ещё амбигуозно
        if current_count > 1:
            logger.warning(f"После narrowing осталось {current_count} элементов")
            logger.warning(f"   Шаги: {self.narrowing_log}")
            
            # Пытаемся последний раз - берем первый видимый в viewport
            try:
                current_locator = await self._get_first_in_viewport(current_locator)
                final_count = await current_locator.count()
                if final_count == 1:
                    self.narrowing_log.append("Выбран первый в viewport")
                    logger.info(f"  🔽 Выбран первый элемент в viewport")
                    current_count = 1
            except:
                pass
        
        return {
            "success": current_count == 1,
            "locator": current_locator,
            "final_count": current_count,
            "narrowing_steps": self.narrowing_log,
            "needs_user_input": current_count > 1  # Если всё ещё >1 - нужен пользователь
        }
    
    async def _apply_scope_narrowing(
        self,
        locator: Locator,
        intent: str
    ) -> Optional[Dict[str, Any]]:
        """
        Ограничить область поиска (main, form, section и т.д.).
        """
        # Ключевые области для narrowing
        scopes = [
            ("main", "главную область контента"),
            ("form", "форму"),
            ("[role='search']", "зону поиска"),
            ("[role='region']", "регион"),
            (".modal", "модальное окно"),
            (".sidebar", "боковую панель"),
        ]
        
        for scope_selector, scope_name in scopes:
            try:
                # Попробовать узко ограничить
                scope_area = self.page.locator(scope_selector)
                if await scope_area.count() > 0:
                    narrowed = scope_area.locator(locator._selector if hasattr(locator, '_selector') else "")
                    # Fallback - use the existing locator with filter
                    # В Playwright это сложнее, использован будет фильтр по has()
                    
                    narrowed_count = await narrowed.count()
                    if 0 < narrowed_count < await locator.count():
                        return {
                            "locator": narrowed,
                            "count": narrowed_count,
                            "reason": f"Сужена область до {scope_name}: {narrowed_count} элементов"
                        }
            except:
                pass
        
        return None
    
    async def _apply_visibility_narrowing(
        self,
        locator: Locator
    ) -> Optional[Dict[str, Any]]:
        """
        Отфильтровать только видимые элементы.
        """
        try:
            # Применить фильтр видимости
            visible_locator = locator.filter(has=self.page.locator(":visible"))
            visible_count = await visible_locator.count()
            total_count = await locator.count()
            
            if visible_count < total_count and visible_count > 0:
                return {
                    "locator": visible_locator,
                    "count": visible_count,
                    "reason": f"Отфильтрованы только видимые: {visible_count} из {total_count}"
                }
        except:
            pass
        
        return None
    
    async def _apply_position_narrowing(
        self,
        locator: Locator,
        intent: str
    ) -> Optional[Dict[str, Any]]:
        """
        Выбрать элемент по позиции (первый в viewport, первый, и т.д.).
        """
        try:
            count = await locator.count()
            
            # Если много элементов - выбрать первый в viewport (обычно самый важный для пользователя)
            if count > 1:
                first_locator = locator.first
                # Проверим что первый в viewport
                is_visible = await first_locator.is_visible()
                if is_visible:
                    return {
                        "locator": first_locator,
                        "count": 1,
                        "reason": "Выбран первый видимый элемент"
                    }
        except:
            pass
        
        return None
    
    async def _get_first_in_viewport(self, locator: Locator) -> Locator:
        """
        Получить первый элемент который находится в viewport.
        """
        try:
            for i in range(min(5, await locator.count())):
                elem = locator.nth(i)
                if await elem.is_in_viewport():
                    return elem.locator("..")  # Вернуть сам элемент
        except:
            pass
        
        # Fallback - просто первый
        return locator.first
