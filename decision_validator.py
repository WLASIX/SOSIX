"""
Decision validation module.
Strict validation of LLM decisions before execution against element capabilities.
"""
from typing import Dict, Any, Tuple, Optional
import json
from logger import logger


class DecisionValidator:
    """Validates LLM decisions against element capabilities"""
    
    @staticmethod
    def parse_decision(decision_str: str) -> Tuple[bool, Optional[Dict], str]:
        """
        Parse decision JSON strictly.
        Handles multiple formats:
        1. Plain JSON: {...}
        2. Markdown code block: ```json ... ```
        3. New format with thinking: ДУМАЮ: ... РЕШЕНИЕ: {...}
        
        Returns: (is_valid, parsed_dict, error_msg)
        """
        if not decision_str:
            return False, None, "Пустой ответ от модели"
        
        clean_str = decision_str.strip()
        
        # Handle new format: ДУМАЮ: ... РЕШЕНИЕ: {...}
        if "решение:" in clean_str.lower():
            import re
            match = re.search(r'решение:\s*({.+})', clean_str, re.IGNORECASE | re.DOTALL)
            if match:
                clean_str = match.group(1).strip()
        
        # Extract JSON from markdown code blocks if present
        if "```" in clean_str:
            # Try to extract content between ```json and ```
            import re
            match = re.search(r'```(?:json)?\s*\n?([^`]+)```', clean_str, re.DOTALL)
            if match:
                clean_str = match.group(1).strip()
        
        try:
            parsed = json.loads(clean_str)
        except json.JSONDecodeError as e:
            return False, None, f"Некорректный JSON: {str(e)[:50]}"
        except Exception as e:
            return False, None, f"Ошибка парсинга: {str(e)[:50]}"
        
        if not isinstance(parsed, dict):
            return False, None, "Решение должно быть объектом JSON"
        
        if "action" not in parsed:
            return False, None, "Отсутствует обязательное поле 'action'"
        
        return True, parsed, ""
    
    @staticmethod
    def validate_action_against_element(action: str, element) -> Tuple[bool, str]:
        """
        Check if action matches element capability.
        Returns: (is_valid, reason)
        """
        action_lower = action.lower()
        
        if action_lower == "click" and not element.can_click:
            reason = element.disabled_reason or "неизвестная причина"
            return False, f"[{element.id}] не кликабельный: {reason}"
        
        if action_lower == "fill" and not element.can_fill:
            reason = element.disabled_reason or "неизвестная причина"
            return False, f"[{element.id}] не заполняемый: {reason}"
        
        if action_lower == "type" and not element.can_type:
            reason = element.disabled_reason or "неизвестная причина"
            return False, f"[{element.id}] не поддерживает ввод: {reason}"
        
        if action_lower == "submit" and not element.can_click:
            return False, f"[{element.id}] кнопка не кликабельна для submit"
        
        return True, ""
    
    @staticmethod
    def validate_full_decision(decision: Dict, page_analysis) -> Tuple[bool, str]:
        """
        v2: Full validation - NEW MODEL with strategy+args instead of elem_id
        
        Validates:
        - action is valid
        - strategy is provided for action that need it
        - args dict is valid
        - value provided for fill/type
        """
        action = (decision.get("action") or "").lower().strip()
        strategy = (decision.get("strategy") or "").lower().strip()  # NEW: strategy instead of target
        args = decision.get("args", {}) or {}  # NEW: locator args
        value = decision.get("value", "")
        
        if isinstance(value, (int, float)):
            value = str(value)
        elif isinstance(value, str):
            value = value.strip()
        else:
            value = ""
        
        valid_actions = [
            "click", "fill", "type", "submit", "scroll", "goto",
            "wait", "ask_user", "wait_for_user_action", "confirm_complete", "press_key"
        ]
        
        if action not in valid_actions:
            return False, f"Неизвестное действие: '{action}'"
        
        # ========== ACTION-SPECIFIC VALIDATIONS ==========
        
        # Actions that need locator (strategy + args)
        locator_actions = ["click", "fill", "type", "submit"]
        
        if action in locator_actions:
            if not strategy:
                return False, f"Действие '{action}' требует 'strategy' (role, text, label, placeholder, aria-label, id)"
            
            valid_strategies = ["role", "text", "label", "placeholder", "css", "aria-label", "id"]
            if strategy not in valid_strategies:
                return False, f"Неизвестная strategy '{strategy}'"
            
            if not args or not isinstance(args, dict):
                return False, f"Действие '{action}' требует 'args' (dict с параметрами локатора)"
            
            # Empty dict - likely error
            if len(args) == 0:
                return False, f"args не может быть пустой для strategy '{strategy}'"
        
        if action in ["fill", "type"]:
            if not value:
                return False, f"Действие '{action}' требует 'value' с текстом"
        
        if action == "goto":
            target = decision.get("target", "")
            if not target:
                return False, "Действие 'goto' требует 'target' с URL"
            if not (target.startswith("http://") or target.startswith("https://")):
                return False, f"URL должен начинаться с http:// или https://"
        
        if action == "wait":
            try:
                wait_ms = int(value or 1000)
                if wait_ms < 0 or wait_ms > 60000:  # Max 60 sec
                    return False, f"Время ожидания должно быть 0-60000мс"
            except (ValueError, TypeError):
                return False, f"Действие 'wait' требует числовое 'value'"
        
        if action == "scroll":
            if value and value.lower() not in ["up", "down"]:
                return False, f"Направление должно быть 'up' или 'down'"
        
        return True, ""
