"""
Main entry point for the autonomous AI browser agent.
Handles command-line interface and agent lifecycle.
"""
import asyncio
import sys
from pathlib import Path
from logger import logger
from config_loader import config
from browser_agent import BrowserAgent


async def main():
    """Main entry point"""
    
    # Configure logging level from config
    logging_config = config.get_logging_config()
    log_level = logging_config.get("level", "INFO")
    logger.set_log_level(log_level)
    
    logger.section("🚀 SOSIX AGENT 🚀")
    
    # Initialize agent
    agent = BrowserAgent()
    
    try:
        await agent.initialize()
        
        # Main loop
        while True:
            logger.info("📋 Ввод задачи")
            print()
            
            # Get task from user
            logger.info("Введите вашу задачу (или 'выход' чтобы выйти):")
            task_description = input("> ").strip()
            
            if task_description.lower() in ['exit', 'quit', 'q', 'выход', 'вых']:
                logger.info("Выход...")
                break
            
            if not task_description:
                logger.warning("Задача не может быть пустой")
                continue
            
            # Execute task
            try:
                result = await agent.execute_task(task_description)
                
                # Display result
                logger.result(f"Статус: {result.get('status')}")
                if result.get('iterations'):
                    logger.result(f"Итераций: {result.get('iterations')}")
                if result.get('summary'):
                    logger.result(f"Итог: {result.get('summary')}")
                if result.get('error'):
                    logger.error(f"Ошибка: {result.get('error')}")
                if result.get('final_url'):
                    logger.result(f"Итоговый URL: {result.get('final_url')}")
                
            except KeyboardInterrupt:
                logger.warning("Выполнение прервано пользователем (Ctrl+C)")
                break
            except Exception as e:
                logger.error(f"Ошибка выполнения задачи: {str(e)}")
            
            print()
    
    except KeyboardInterrupt:
        logger.warning("Инициализация прервана пользователем (Ctrl+C)")
    except Exception as e:
        logger.error(f"Критическая ошибка: {str(e)}")
    finally:
        await agent.shutdown()
        logger.success("✅ Агент остановлен")


if __name__ == "__main__":
    # Fix for async on Windows
    if sys.platform == "win32":
        # Use ProactorEventLoop on Windows so subprocesses (Playwright) work
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except AttributeError:
            # Fallback if policy not available in this Python build
            pass
    
    asyncio.run(main())
