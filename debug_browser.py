import asyncio
from playwright.async_api import async_playwright
import logging
import os
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("browser_debug")

async def run():
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    
    async with async_playwright() as p:
        logger.info("Launching browser (non-headless)...")
        try:
            browser = await p.chromium.launch(executable_path=chrome_path, headless=True, args=['--no-sandbox'])
            context = await browser.new_context()
            page = await context.new_page()
            
            page.on("console", lambda msg: logger.info(f"BROWSER CONSOLE: {msg.text}"))
            
            # 1. Navigate to App
            logger.info("Navigating to http://127.0.0.1:5001 ...")
            await page.goto("http://127.0.0.1:5001", timeout=60000)
            
            # 2. Login
            await page.wait_for_selector('#email', timeout=10000)
            await page.fill('#email', "test3@gmail.com")
            await page.fill('#password', "123")
            await page.click('#submit-btn')
            
            # 3. Wait for dashboard
            logger.info("Waiting for dashboard...")
            await page.wait_for_selector('#user-ui-container', timeout=15000)
            await page.evaluate("document.getElementById('user-ui-container').classList.remove('hidden')")
            
            await asyncio.sleep(5) # Wait for multiple snapshots
            
            # 4. Check for slots in DOM
            slots = await page.query_selector_all('.slot-item')
            logger.info(f"UI Slots Found in DOM: {len(slots)}")
            
            # 5. Extract appState from window for deep inspection
            state = await page.evaluate("window.__SEVCS_DEBUG__.getState()")
            snapshot = state.get('snapshot')
            if snapshot:
                logger.info(f"SNAPSHOT SEQUENCE: {snapshot.get('snapshot_sequence')}")
                logger.info(f"SNAPSHOT VERSION: {snapshot.get('snapshot_version')}")
                logger.info(f"SLOTS IN SNAPSHOT: {len(snapshot.get('slots', []))}")
                logger.info(f"DEV_MODE IN SNAPSHOT: {snapshot.get('dev_mode')}")
            else:
                logger.error("NO SNAPSHOT FOUND IN APP STATE")

            await page.screenshot(path="slot_visibility_check.png")
            logger.info("Screenshot saved as slot_visibility_check.png")
            
            await browser.close()
            logger.info("Browser test completed.")
        except Exception as e:
            logger.error(f"CRITICAL: Failed during browser test: {e}")

if __name__ == "__main__":
    asyncio.run(run())
