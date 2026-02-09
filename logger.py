"""
Rich logging module with colors and emojis.
Provides structured logging for all agent operations.
"""
import sys
import json
import inspect
import threading
from datetime import datetime
from enum import Enum
from colorama import Fore, Back, Style, init

init(autoreset=True)


class LogLevel(Enum):
    """Log level enumeration"""
    # Основные уровни логирования с яркими цветами
    ERROR = ("❌", Fore.RED + Style.BRIGHT)  # Ошибки - красный яркий
    WARNING = ("🚨", Fore.YELLOW + Style.BRIGHT)  # Предупреждения - желтый яркий
    INFO = ("📋", Fore.WHITE + Style.BRIGHT)  # Нормальный вывод - белый яркий
    SUCCESS = ("✅", Fore.GREEN + Style.BRIGHT)  # Успех - зеленый яркий
    
    # Информационные уровни
    LLM = ("🧠", Fore.WHITE)
    ANALYSIS = ("🔍", Fore.WHITE)
    THINK = ("💭", Fore.WHITE)
    ACTION = ("🎯", Fore.WHITE)
    NAVIGATION = ("🌍", Fore.WHITE)
    WAIT = ("⏳", Fore.WHITE)
    SECURITY = ("🔒", Fore.RED + Style.BRIGHT)  # Безопасность - красный яркий
    DECISION = ("💡", Fore.WHITE)
    TOOL = ("🛠️", Fore.MAGENTA)
    RESULT = ("📊", Fore.GREEN)
    LLM_ANALYSIS = ("🧠", Fore.WHITE)
    DOM = ("🌳", Fore.WHITE)
    DEBUG = ("🔧", Fore.WHITE)


class LogLevelFilter(Enum):
    """Фильтр уровней логирования"""
    DEBUG = 0      # Все сообщения
    INFO = 1       # INFO и выше (без DEBUG)
    WARNING = 2    # WARNING и выше
    ERROR = 3      # Только ERROR


class AgentLogger:
    """Logger for agent operations with rich formatting"""

    def __init__(self, log_level: str = "INFO"):
        self.log_level = self._parse_log_level(log_level)
        self.indent_level = 0
        self._print_lock = threading.Lock()  # Synchronize output from async code

    @staticmethod
    def _parse_log_level(level_str: str) -> LogLevelFilter:
        """Преобразовать строку уровня в enum"""
        level_map = {
            "DEBUG": LogLevelFilter.DEBUG,
            "INFO": LogLevelFilter.INFO,
            "WARNING": LogLevelFilter.WARNING,
            "ERROR": LogLevelFilter.ERROR
        }
        return level_map.get(level_str.upper(), LogLevelFilter.INFO)

    def set_log_level(self, level: str):
        """Установить уровень логирования"""
        self.log_level = self._parse_log_level(level)

    def _should_log_debug(self) -> bool:
        """Проверить, нужно ли логировать DEBUG сообщения"""
        return self.log_level == LogLevelFilter.DEBUG

    def _should_log_info(self) -> bool:
        """Проверить, нужно ли логировать INFO сообщения и выше"""
        return self.log_level <= LogLevelFilter.INFO

    def _format_message(self, level: LogLevel, message: str, prefix: str = None) -> str:
        """Format message with timestamp, emoji, and color"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        emoji, color = level.value
        indent = "  " * self.indent_level
        
        # Format: emoji [HH:MM:SS] [PREFIX] message
        ts = f"{Style.DIM}[{timestamp}]{Style.RESET_ALL}"
        
        if prefix:
            prefix_label = f"{Style.BRIGHT}{Fore.CYAN}[{prefix}]{Style.RESET_ALL}"
            return f"{indent}{emoji} {ts} {prefix_label} {color}{message}{Style.RESET_ALL}"
        return f"{indent}{emoji} {ts} {color}{message}{Style.RESET_ALL}"

    def _print_with_flush(self, text: str):
        """Print with immediate flush and lock to prevent buffer misalignment and race conditions"""
        with self._print_lock:
            print(text, flush=True)

    # ========== MAIN LOG METHODS ==========
    
    def llm(self, message: str):
        """Log LLM action/response"""
        self._print_with_flush(self._format_message(LogLevel.LLM, message, "LLM"))

    def analysis(self, message: str):
        """Log analysis step"""
        self._print_with_flush(self._format_message(LogLevel.ANALYSIS, message, "INFO"))

    def think(self, message: str):
        """Log thinking/reasoning step"""
        self._print_with_flush(self._format_message(LogLevel.THINK, message, "THINK"))

    def action(self, message: str):
        """Log action step"""
        self._print_with_flush(self._format_message(LogLevel.ACTION, message, "ACTION"))

    def navigation(self, message: str):
        """Log navigation step"""
        self._print_with_flush(self._format_message(LogLevel.NAVIGATION, message, "NAVIGATION"))

    def dom(self, message: str):
        """Log DOM analysis"""
        self._print_with_flush(self._format_message(LogLevel.DOM, message, "DOM"))

    def wait(self, message: str):
        """Log waiting step"""
        self._print_with_flush(self._format_message(LogLevel.WAIT, message, "WAIT"))

    def success(self, message: str):
        """Log success"""
        self._print_with_flush(self._format_message(LogLevel.SUCCESS, message, "SUCCESS"))

    def error(self, message: str):
        """Log error"""
        self._print_with_flush(self._format_message(LogLevel.ERROR, message, "ERROR"))

    def info(self, message: str):
        """Log info"""
        self._print_with_flush(self._format_message(LogLevel.INFO, message, "INFO"))

    def debug(self, message: str):
        """Log debug information"""
        if self._should_log_debug():
            self._print_with_flush(self._format_message(LogLevel.DEBUG, message, "DEBUG"))

    def warning(self, message: str):
        """Log warning"""
        self._print_with_flush(self._format_message(LogLevel.WARNING, message, "WARNING"))

    def security_prompt(self, message: str):
        """Log security confirmation request"""
        self._print_with_flush(self._format_message(LogLevel.SECURITY, message, "SECURITY"))

    def decision(self, message: str):
        """Log decision making step"""
        self._print_with_flush(self._format_message(LogLevel.DECISION, message, "THINK"))

    def result(self, message: str):
        """Log final result"""
        self._print_with_flush(self._format_message(LogLevel.RESULT, message, "RESULT"))

    # ========== SECTION & FORMATTING METHODS ==========
    
    def section(self, title: str):
        """Log section header with pretty formatting"""
        border = "=" * 60
        section_output = [
            "",
            f"{Fore.CYAN}{border}",
            f"{Fore.CYAN}>>> {title}",
            f"{Fore.CYAN}{border}{Style.RESET_ALL}"
        ]
        self._print_with_flush("\n".join(section_output))

    def subsection(self, title: str):
        """Log subsection header"""
        output = f"\n{Fore.BLUE}━━━ {title} ━━━{Style.RESET_ALL}\n"
        self._print_with_flush(output)

    def log_config(self, config_dict: dict, title: str = "КОНФИГУРАЦИЯ"):
        """Log configuration in pretty format"""
        self.section(f"⚙️ {title}")
        for key, value in config_dict.items():
            self.info(f"{key}: {value}")

    def log_stats(self, stats_dict: dict):
        """Log statistics/results in formatted table"""
        self.section("📊 СТАТИСТИКА")
        for key, value in stats_dict.items():
            self.result(f"{key}: {value}")

    def tool_call(self, tool_name: str, description: str = None):
        """Log tool call with brief description"""
        if description:
            msg = f"Вызов: {tool_name} — {description}"
        else:
            msg = f"Вызов: {tool_name}"
        header = self._format_message(LogLevel.TOOL, msg, "TOOL")
        self._print_with_flush(header)

    def start(self, note: str = None):
        """Log entering a function"""
        stack = inspect.stack()
        if len(stack) > 1:
            caller = stack[1]
            filename = caller.filename
            func = caller.function
        else:
            filename = '<unknown>'
            func = '<module>'
        header = f"Функция: {func} ({filename})"
        if note:
            header += f" — {note}"
        self._print_with_flush(self._format_message(LogLevel.INFO, header, "START"))

    def indent(self):
        """Increase indentation level"""
        self.indent_level += 1

    def dedent(self):
        """Decrease indentation level"""
        if self.indent_level > 0:
            self.indent_level -= 1

    # ========== USER INTERACTION ==========
    
    def ask_user(self, question: str) -> str:
        """Ask user for input"""
        self._print_with_flush("")
        self._print_with_flush(f"{Fore.YELLOW}❓ {question}")
        return input(f"{Fore.GREEN}> {Style.RESET_ALL}").strip()

    def confirm(self, message: str) -> bool:
        """Ask user for confirmation"""
        self._print_with_flush("")
        self._print_with_flush(f"{Fore.MAGENTA}🔒 {message}")
        response = input(f"{Fore.YELLOW}[Y/N] > {Style.RESET_ALL}").strip().lower()
        return response in ['y', 'yes']

    # ========== LLM COMMUNICATION ==========
    
    def llm_prompt_sent(self, function_name: str, question: str):
        """Log LLM question being sent - shows what the model is being asked
        
        Args:
            function_name: Name of the function (e.g., 'check_if_task_complete')
            question: Brief question/task for the model (not full prompt)
        """
        self.section(f"🧠 ЗАПРОС К МОДЕЛИ [{function_name}]")
        
        # Log brief question only
        self._print_with_flush(f"{Fore.YELLOW}👤 ВОПРОС:{Style.RESET_ALL}")
        indent_str = "  "
        for line in question.splitlines():
            self._print_with_flush(f"{indent_str}{Fore.YELLOW}{line}{Style.RESET_ALL}")

    def llm_response_received(self, response_text: str, function_name: str = ""):
        """Log full LLM response - shows what the model decided"""
        func_label = f" [{function_name}]" if function_name else ""
        self.section(f"💬 ОТВЕТ МОДЕЛИ{func_label}")
        
        # Log response
        indent_str = "  "
        response_lines = response_text.splitlines()
        
        # Show all lines (no truncation for responses)
        for line in response_lines:
            if line.startswith('{') or line.startswith('[') or line.strip().startswith('"'):
                # Highlight JSON
                self._print_with_flush(f"{indent_str}{Fore.GREEN}{line}{Style.RESET_ALL}")
            else:
                self._print_with_flush(f"{indent_str}{Fore.WHITE}{line}{Style.RESET_ALL}")
        
        if not response_lines:
            self._print_with_flush(f"{indent_str}{Fore.RED}[Empty response]{Style.RESET_ALL}")

    # ========== SUMMARY/FINAL OUTPUT ==========
    
    def task_summary(self, task_desc: str, goal: str, task_type: str, risk: bool):
        """Log task summary at startup"""
        self.section("📝 АНАЛИЗ ЗАДАЧИ")
        self.info(f"Задача: {task_desc}")
        self.info(f"Цель: {goal}")
        self.info(f"Тип: {task_type}")
        self.info(f"Риск: {'⚠️ ДА' if risk else '✅ НЕТ'}")

    def page_analysis(self, url: str, elements_count: int, interactive_count: int):
        """Log page analysis results"""
        self.section("🔎 АНАЛИЗ СТРАНИЦЫ")
        self.info(f"URL: {url}")
        self.info(f"DOM элементов всего: {elements_count}")
        self.info(f"Потенциально интерактивных: {interactive_count}")

    def task_result(self, status: str, summary: str = None, url: str = None):
        """Log final task result"""
        self.section("📊 РЕЗУЛЬТАТ")
        self.result(f"Статус: {status}")
        if summary:
            self.result(f"Итог: {summary}")
        if url:
            self.result(f"URL: {url}")


# Global logger instance
logger = AgentLogger(log_level="INFO")
