"""
Minimal Browser Automation - Click Interactive Elements Only
Returns static HTML for existing extractors to process

Purpose: Handle ONLY the clicking/interaction, not extraction
- Click "International" toggles
- Click "Entry Requirements" tabs
- Expand collapsed accordions
- Return final HTML to existing extractors
"""

import asyncio
from typing import Dict, List, Optional
from playwright.async_api import async_playwright, Page, Browser
import re


class MinimalBrowserHelper:
    """
    Minimal browser automation - ONLY for clicking elements
    
    Does NOT do extraction - that's handled by existing static HTML extractors
    
    Workflow:
    1. Load page
    2. Click interactive elements (toggles, tabs, accordions)
    3. Return final HTML
    4. Existing extractors process the HTML
    """
    
    def __init__(self):
        # Patterns for detecting interactive elements
        self.international_selectors = [
            'text="International"',
            'text="International Students"',
            'text="Overseas Students"',
            '[data-student-type="international"]',
            'button:has-text("International")',
            '.international-toggle',
            '#international-btn',
        ]
        
        self.requirements_tab_selectors = [
            'text="Entry Requirements"',
            'text="Admission Requirements"',
            'a:has-text("Entry Requirements")',
            '[href*="requirements"]',
        ]
        
        self.accordion_patterns = [
            'Minimum English Language Requirement',
            'English Language Requirements',
            'English Requirements',
            'Language Requirements',
        ]
    
    
    async def get_interactive_html(
        self, 
        url: str,
        click_international: bool = True,
        click_requirements_tab: bool = True,
        expand_accordions: bool = True,
        timeout: int = 30000
    ) -> Dict[str, str]:
        """
        Load page, click interactive elements, return HTML
        
        Args:
            url: Course page URL
            click_international: Try to click International toggle
            click_requirements_tab: Try to navigate to requirements tab
            expand_accordions: Try to expand collapsed sections
            timeout: Page load timeout in ms
        
        Returns:
            {
                'main_html': HTML after clicking international toggle,
                'requirements_html': HTML of requirements section (if tab clicked),
                'clicks_performed': List of what was clicked
            }
        """
        
        clicks_performed = []
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )
            
            page = await browser.new_page()
            
            try:
                # ═══════════════════════════════════════════════════
                # STEP 1: Load page
                # ═══════════════════════════════════════════════════
                
                print(f"[Browser] Loading {url}")
                await page.goto(url, wait_until='networkidle', timeout=timeout)
                await page.wait_for_timeout(2000)  # Let JS render
                
                # ═══════════════════════════════════════════════════
                # STEP 2: Click International toggle (if exists)
                # ═══════════════════════════════════════════════════
                
                if click_international:
                    clicked = await self._try_click_international(page)
                    if clicked:
                        clicks_performed.append('international_toggle')
                        await page.wait_for_timeout(2000)  # Wait for content update
                
                # Get main page HTML (with international view if toggled)
                main_html = await page.content()
                
                # ═══════════════════════════════════════════════════
                # STEP 3: Navigate to Entry Requirements tab (if exists)
                # ═══════════════════════════════════════════════════
                
                requirements_html = None
                
                if click_requirements_tab:
                    clicked = await self._try_click_requirements_tab(page)
                    if clicked:
                        clicks_performed.append('requirements_tab')
                        await page.wait_for_timeout(1000)
                        
                        # ═══════════════════════════════════════════
                        # STEP 4: Expand accordions in requirements section
                        # ═══════════════════════════════════════════
                        
                        if expand_accordions:
                            expanded = await self._try_expand_accordions(page)
                            if expanded:
                                clicks_performed.append(f'accordions_expanded_{len(expanded)}')
                                await page.wait_for_timeout(500)
                        
                        # Get requirements HTML
                        requirements_html = await page.content()
                
                await browser.close()
                
                return {
                    'main_html': main_html,
                    'requirements_html': requirements_html,
                    'clicks_performed': clicks_performed
                }
            
            except Exception as e:
                print(f"[Browser] Error: {e}")
                await browser.close()
                
                # Return empty on error - caller will fallback to static
                return {
                    'main_html': '',
                    'requirements_html': None,
                    'clicks_performed': [],
                    'error': str(e)
                }
    
    
    async def _try_click_international(self, page: Page) -> bool:
        """
        Try to click International student toggle
        
        Returns True if clicked, False if not found
        """
        
        for selector in self.international_selectors:
            try:
                element = await page.wait_for_selector(selector, timeout=3000)
                
                if element:
                    # Check if visible
                    is_visible = await element.is_visible()
                    
                    if is_visible:
                        # Check if already selected
                        is_selected = await self._is_already_selected(page, element)
                        
                        if not is_selected:
                            print(f"[Browser] Clicking: {selector}")
                            await element.click()
                            return True
                        else:
                            print(f"[Browser] Already selected: {selector}")
                            return True
            
            except Exception as e:
                # This selector didn't work, try next
                continue
        
        print("[Browser] No International toggle found")
        return False
    
    
    async def _is_already_selected(self, page: Page, element) -> bool:
        """Check if element is already selected/active"""
        
        try:
            # Check common "selected" indicators
            class_name = await element.get_attribute('class') or ''
            aria_selected = await element.get_attribute('aria-selected')
            
            if 'active' in class_name or 'selected' in class_name:
                return True
            
            if aria_selected == 'true':
                return True
        
        except:
            pass
        
        return False
    
    
    async def _try_click_requirements_tab(self, page: Page) -> bool:
        """
        Try to click Entry Requirements tab/link
        
        Returns True if clicked, False if not found
        """
        
        for selector in self.requirements_tab_selectors:
            try:
                element = await page.wait_for_selector(selector, timeout=3000)
                
                if element:
                    is_visible = await element.is_visible()
                    
                    if is_visible:
                        print(f"[Browser] Clicking: {selector}")
                        await element.click()
                        return True
            
            except:
                continue
        
        print("[Browser] No Entry Requirements tab found")
        return False
    
    
    async def _try_expand_accordions(self, page: Page) -> List[str]:
        """
        Try to expand collapsed accordions
        
        Returns list of expanded accordion names
        """
        
        expanded = []
        
        for pattern in self.accordion_patterns:
            try:
                # Find elements with this text
                elements = await page.query_selector_all(f'text="{pattern}"')
                
                for element in elements:
                    is_visible = await element.is_visible()
                    
                    if is_visible:
                        # Check if it's clickable (accordion header)
                        tag_name = await element.evaluate('el => el.tagName')
                        
                        if tag_name.lower() in ['button', 'a', 'div', 'summary']:
                            print(f"[Browser] Expanding: {pattern}")
                            await element.click()
                            expanded.append(pattern)
                            await page.wait_for_timeout(500)
            
            except Exception as e:
                continue
        
        if expanded:
            print(f"[Browser] Expanded {len(expanded)} accordions")
        else:
            print("[Browser] No accordions to expand")
        
        return expanded


# ═══════════════════════════════════════════════════════════════
# INTEGRATION WITH EXISTING SCRAPER
# ═══════════════════════════════════════════════════════════════

async def extract_course_with_browser_assist(
    url: str,
    country: str,
    existing_extractors: Dict  # Your existing fee/duration/IELTS extractors
) -> Dict:
    """
    Use browser to handle interactions, then pass to existing extractors
    
    Workflow:
    1. Browser clicks interactive elements
    2. Returns static HTML
    3. Existing extractors process HTML (NO CHANGES NEEDED)
    
    Args:
        url: Course URL
        country: University country
        existing_extractors: Your current extraction functions
    
    Returns:
        Complete course data
    """
    
    browser_helper = MinimalBrowserHelper()
    
    # ═══════════════════════════════════════════════════════════
    # PHASE 1: Get HTML with browser interactions
    # ═══════════════════════════════════════════════════════════
    
    result = await browser_helper.get_interactive_html(
        url,
        click_international=True,
        click_requirements_tab=True,
        expand_accordions=True
    )
    
    print(f"[Browser] Clicks performed: {result['clicks_performed']}")
    
    # ═══════════════════════════════════════════════════════════
    # PHASE 2: Pass HTML to existing extractors
    # ═══════════════════════════════════════════════════════════
    
    main_html = result['main_html']
    requirements_html = result['requirements_html'] or main_html
    
    course_data = {'course_url': url}
    
    # Use your EXISTING extractors (no changes needed!)
    course_data.update(
        existing_extractors['fee_extractor'](main_html, country)
    )
    
    course_data.update(
        existing_extractors['duration_extractor'](main_html)
    )
    
    course_data.update(
        existing_extractors['study_mode_extractor'](main_html)
    )
    
    # Extract IELTS from requirements HTML (after tab clicked & accordion expanded)
    course_data.update(
        existing_extractors['english_extractor'](requirements_html)
    )
    
    return course_data


# ═══════════════════════════════════════════════════════════════
# ORCHESTRATOR INTEGRATION
# ═══════════════════════════════════════════════════════════════

async def extract_course_smart(
    url: str,
    university: Dict,
    existing_extractors: Dict
) -> Dict:
    """
    Smart extraction - decides when to use browser
    
    Decision logic:
    - VIT: Always use browser (has toggles/tabs/accordions)
    - Others: Try static first, fallback to browser if incomplete
    """
    
    # ═══════════════════════════════════════════════════════════
    # OPTION 1: Always use browser for VIT
    # ═══════════════════════════════════════════════════════════
    
    if 'vit.edu.au' in url:
        print(f"[Smart] VIT detected - using browser")
        return await extract_course_with_browser_assist(
            url,
            university['country'],
            existing_extractors
        )
    
    # ═══════════════════════════════════════════════════════════
    # OPTION 2: Try static first for others
    # ═══════════════════════════════════════════════════════════
    
    # Try with static HTML first
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as response:
                html = await response.text()
        
        # Extract with existing extractors
        course_data = {}
        course_data.update(existing_extractors['fee_extractor'](html, university['country']))
        course_data.update(existing_extractors['duration_extractor'](html))
        course_data.update(existing_extractors['english_extractor'](html))
        
        # Check completeness
        is_complete = (
            course_data.get('international_fee') and
            course_data.get('duration') and
            course_data.get('ielts_overall')
        )
        
        if is_complete:
            print(f"[Smart] Static HTML worked - complete data")
            return course_data
        else:
            print(f"[Smart] Static HTML incomplete - trying browser")
            return await extract_course_with_browser_assist(
                url,
                university['country'],
                existing_extractors
            )
    
    except Exception as e:
        print(f"[Smart] Static HTML failed - using browser: {e}")
        return await extract_course_with_browser_assist(
            url,
            university['country'],
            existing_extractors
        )


# ═══════════════════════════════════════════════════════════════
# EXAMPLE USAGE
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    """
    Test with VIT course
    """
    
    import asyncio
    
    async def test_vit():
        # Mock existing extractors (replace with your actual extractors)
        existing_extractors = {
            'fee_extractor': lambda html, country: {'international_fee': 48000, 'fee_term': 'Full Course'},
            'duration_extractor': lambda html: {'duration': 2, 'duration_term': 'Years'},
            'study_mode_extractor': lambda html: {'study_mode': 'Blended'},
            'english_extractor': lambda html: {'ielts_overall': 6.5, 'ielts_listening': 6.0}
        }
        
        # Test VIT MBA
        url = 'https://vit.edu.au/mba/mba-project-management'
        university = {'country': 'Australia'}
        
        result = await extract_course_smart(url, university, existing_extractors)
        
        print("\n" + "="*60)
        print("VIT EXTRACTION RESULT")
        print("="*60)
        print(f"URL: {url}")
        print(f"Fee: ${result.get('international_fee'):,.0f} ({result.get('fee_term')})")
        print(f"Duration: {result.get('duration')} {result.get('duration_term')}")
        print(f"Study Mode: {result.get('study_mode')}")
        print(f"IELTS: {result.get('ielts_overall')} (min {result.get('ielts_listening')})")
        print("="*60)
    
    asyncio.run(test_vit())
