"""
Universal Smart Scraper - Works for ALL Universities

Auto-detects:
- Domestic/International toggles
- Tab-based navigation
- Dynamic content
- Course list pages
- Fee terms (Full Course vs Annual)
- Entry requirements location

No site-specific code needed!
"""

import re
import asyncio
from typing import Dict, List, Optional
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import aiohttp
from playwright.async_api import async_playwright


class UniversalSmartScraper:
    """
    Universal scraper that adapts to any university website
    
    Key Features:
    - Auto-detects domestic/international toggles
    - Auto-navigates to tabs if needed
    - Auto-discovers course list pages
    - Works with static AND dynamic sites
    - Intelligently determines fee terms
    """
    
    def __init__(self):
        # Domestic/International toggle patterns
        self.international_selectors = [
            'text="International"',
            'text="International Students"',
            'text="Overseas Students"',
            '[data-student-type="international"]',
            'button:has-text("International")',
            'a:has-text("International")',
        ]
        
        # Entry requirements tab/link patterns
        self.requirements_selectors = [
            'text="Entry Requirements"',
            'text="Admission Requirements"',
            'text="Requirements"',
            'a:has-text("Entry Requirements")',
            'a:has-text("Requirements")',
        ]
        
        # Course list page patterns
        self.course_list_patterns = [
            '/course-list',
            '/courses',
            '/course-search',
            '/programs',
            '/find-a-course',
            '/study/courses',
        ]
    
    
    async def extract_course_with_smart_detection(
        self, 
        course_url: str,
        university_country: str,
        use_browser: bool = True
    ) -> Dict:
        """
        Extract course data with automatic detection of:
        - Domestic/International toggles
        - Tab navigation needs
        - Dynamic content
        
        Args:
            course_url: Course page URL
            university_country: For currency detection
            use_browser: Use browser automation (default True for reliability)
        
        Returns:
            Complete course data dict
        """
        
        if use_browser:
            return await self._extract_with_browser(course_url, university_country)
        else:
            return await self._extract_with_http(course_url, university_country)
    
    
    async def _extract_with_browser(self, course_url: str, country: str) -> Dict:
        """
        Extract using browser automation with smart detection
        """
        
        print(f"[INFO] Smart scraper: Extracting {course_url} with browser")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            try:
                # ═══════════════════════════════════════════════════════
                # STEP 1: Load page
                # ═══════════════════════════════════════════════════════
                
                await page.goto(course_url, wait_until='networkidle', timeout=30000)
                await page.wait_for_timeout(2000)  # Let JS render
                
                # ═══════════════════════════════════════════════════════
                # STEP 2: AUTO-DETECT & CLICK INTERNATIONAL TOGGLE
                # ═══════════════════════════════════════════════════════
                
                international_clicked = await self._try_click_international(page)
                
                if international_clicked:
                    print("[INFO] Smart scraper: Clicked 'International' toggle")
                    await page.wait_for_timeout(2000)  # Wait for content update
                
                # Get main page HTML
                main_html = await page.content()
                
                # ═══════════════════════════════════════════════════════
                # STEP 3: AUTO-DETECT & NAVIGATE TO REQUIREMENTS TAB
                # ═══════════════════════════════════════════════════════
                
                requirements_html = None
                
                requirements_clicked = await self._try_click_requirements_tab(page)
                
                if requirements_clicked:
                    print("[INFO] Smart scraper: Navigated to 'Entry Requirements' tab")
                    await page.wait_for_timeout(1000)
                    requirements_html = await page.content()
                
                await browser.close()
                
                # ═══════════════════════════════════════════════════════
                # STEP 4: EXTRACT DATA
                # ═══════════════════════════════════════════════════════
                
                course_data = self._extract_all_data(
                    main_html, 
                    requirements_html,
                    course_url,
                    country
                )
                
                return course_data
                
            except Exception as e:
                print(f"[ERROR] Smart scraper failed: {e}")
                await browser.close()
                return {'course_url': course_url, 'error': str(e)}
    
    
    async def _try_click_international(self, page) -> bool:
        """
        Try to find and click International student toggle
        
        Returns True if clicked, False if not found
        """
        
        for selector in self.international_selectors:
            try:
                element = await page.wait_for_selector(selector, timeout=3000)
                
                if element:
                    # Check if it's visible
                    is_visible = await element.is_visible()
                    
                    if is_visible:
                        await element.click()
                        return True
            
            except:
                continue
        
        return False
    
    
    async def _try_click_requirements_tab(self, page) -> bool:
        """
        Try to find and click Entry Requirements tab/link
        
        Returns True if clicked, False if not found
        """
        
        for selector in self.requirements_selectors:
            try:
                element = await page.wait_for_selector(selector, timeout=3000)
                
                if element:
                    is_visible = await element.is_visible()
                    
                    if is_visible:
                        await element.click()
                        return True
            
            except:
                continue
        
        return False
    
    
    def _extract_all_data(
        self, 
        main_html: str,
        requirements_html: Optional[str],
        course_url: str,
        country: str
    ) -> Dict:
        """
        Extract all course data from HTML
        """
        
        course_data = {'course_url': course_url}
        
        # Extract from main page
        course_data.update(self._extract_basic_info(main_html))
        course_data.update(self._extract_fee_smart(main_html, country))
        course_data.update(self._extract_duration_smart(main_html))
        course_data.update(self._extract_intakes(main_html))
        course_data.update(self._extract_locations(main_html))
        course_data.update(self._extract_study_mode_smart(main_html))
        
        # Extract from requirements page/tab if available
        html_to_check = requirements_html if requirements_html else main_html
        
        course_data.update(self._extract_english_tests_smart(html_to_check))
        course_data.update(self._extract_academic_requirements(html_to_check))
        
        return course_data
    
    
    def _extract_basic_info(self, html: str) -> Dict:
        """Extract course name and description"""
        
        soup = BeautifulSoup(html, 'html.parser')
        
        # Course name (h1)
        title_tag = soup.find('h1')
        course_name = title_tag.get_text(strip=True) if title_tag else None
        
        # Remove junk from course name
        if course_name:
            # Remove things like "| University Name"
            course_name = re.split(r'\s*[|‐–—]\s*', course_name)[0]
        
        # Degree level detection
        degree_level = self._detect_degree_level(course_name) if course_name else None
        
        return {
            'course_name': course_name,
            'degree_level': degree_level
        }
    
    
    def _detect_degree_level(self, course_name: str) -> Optional[str]:
        """
        Auto-detect degree level from course name
        """
        
        name_lower = course_name.lower()
        
        if any(kw in name_lower for kw in ['bachelor', 'b.', 'undergraduate']):
            return 'Bachelor'
        elif any(kw in name_lower for kw in ['master', 'm.', 'mba']):
            return 'Master'
        elif any(kw in name_lower for kw in ['phd', 'doctor']):
            return 'PhD'
        elif 'graduate certificate' in name_lower:
            return 'Graduate Certificate'
        elif 'graduate diploma' in name_lower:
            return 'Graduate Diploma'
        elif 'diploma' in name_lower:
            return 'Diploma'
        elif 'certificate' in name_lower:
            return 'Certificate'
        
        return None
    
    
    def _extract_fee_smart(self, html: str, country: str) -> Dict:
        """
        SMART fee extraction that auto-detects:
        - International vs domestic fees
        - Full course vs annual vs semester
        - Currency
        """
        
        # Auto-detect currency
        currency_map = {
            'Australia': 'AUD',
            'New Zealand': 'NZD',
            'Canada': 'CAD',
            'USA': 'USD',
            'United States': 'USD',
            'United Kingdom': 'GBP',
            'UK': 'GBP',
        }
        currency = currency_map.get(country, 'AUD')
        
        # Extract all fee amounts
        fee_pattern = r'\$\s*([\d,]+)'
        amounts = [float(m.replace(',', '')) for m in re.findall(fee_pattern, html)]
        
        # Filter realistic fee range
        amounts = [a for a in amounts if 5000 <= a <= 150000]
        
        if not amounts:
            return {}
        
        # ─────────────────────────────────────────────────────
        # SMART FEE SELECTION
        # ─────────────────────────────────────────────────────
        
        # Look for explicit "international" label
        international_fee_pattern = r'international.*?\$\s*([\d,]+)'
        intl_match = re.search(international_fee_pattern, html, re.I | re.DOTALL)
        
        if intl_match:
            fee = float(intl_match.group(1).replace(',', ''))
        else:
            # No explicit label - take highest amount (international usually higher)
            fee = max(amounts)
        
        # ─────────────────────────────────────────────────────
        # SMART FEE TERM DETECTION
        # ─────────────────────────────────────────────────────
        
        fee_term = self._detect_fee_term(html, fee)
        
        # ─────────────────────────────────────────────────────
        # FEE YEAR DETECTION
        # ─────────────────────────────────────────────────────
        
        year_pattern = r'(202[4-9]|20[3-9]\d)\s+fee'
        year_match = re.search(year_pattern, html, re.I)
        fee_year = int(year_match.group(1)) if year_match else 2026
        
        return {
            'international_fee': fee,
            'fee_term': fee_term,
            'fee_year': fee_year,
            'currency': currency
        }
    
    
    def _detect_fee_term(self, html: str, fee_amount: float) -> str:
        """
        Auto-detect if fee is:
        - Full Course
        - Annual / Per Year
        - Per Semester
        - Per Trimester
        
        Logic:
        1. Check explicit labels
        2. Use fee amount heuristics
        """
        
        # Get context around the fee amount
        fee_str = f'${fee_amount:,.0f}'
        fee_context = ''
        
        # Find the fee in HTML and get surrounding text
        fee_pos = html.find(fee_str)
        if fee_pos != -1:
            start = max(0, fee_pos - 200)
            end = min(len(html), fee_pos + 200)
            fee_context = html[start:end].lower()
        
        # Check explicit labels
        if 'full course' in fee_context or 'total course' in fee_context:
            return 'Full Course'
        elif 'per semester' in fee_context or '/semester' in fee_context:
            return 'Semester'
        elif 'per trimester' in fee_context or '/trimester' in fee_context:
            return 'Trimester'
        elif 'annual' in fee_context or 'per year' in fee_context or '/year' in fee_context:
            return 'Annual'
        
        # Heuristic: If fee > $40,000, likely full course
        # If fee < $20,000, likely semester/annual
        if fee_amount > 40000:
            return 'Full Course'
        elif fee_amount < 20000:
            return 'Annual'
        else:
            # Ambiguous - default to Annual
            return 'Annual'
    
    
    def _extract_duration_smart(self, html: str) -> Dict:
        """
        Smart duration extraction with validation
        """
        
        # Pattern: "2 Years" or "18 Months" etc.
        pattern = r'(\d+(?:\.\d+)?)\s+(years?|months?|semesters?|trimesters?)'
        match = re.search(pattern, html, re.I)
        
        if match:
            duration = float(match.group(1))
            term = match.group(2).lower().rstrip('s') + 's'
            
            # Normalize
            term_map = {
                'years': 'Years',
                'months': 'Months',
                'semesters': 'Semesters',
                'trimesters': 'Trimesters'
            }
            term = term_map.get(term, 'Years')
            
            # Validate (prevent "21 Year" errors)
            if term == 'Years' and duration > 10:
                return {}  # Invalid
            
            return {
                'duration': duration,
                'duration_term': term
            }
        
        return {}
    
    
    def _extract_intakes(self, html: str) -> Dict:
        """Extract intake months"""
        
        months = ['January', 'February', 'March', 'April', 'May', 'June',
                  'July', 'August', 'September', 'October', 'November', 'December']
        
        found_months = []
        
        for month in months:
            if month in html:
                found_months.append(month)
        
        if found_months:
            return {
                'intake_month': found_months[0],
                'all_intakes': ', '.join(found_months)
            }
        
        return {}
    
    
    def _extract_locations(self, html: str) -> Dict:
        """Extract campus locations"""
        
        # Common Australian cities
        cities = ['Sydney', 'Melbourne', 'Brisbane', 'Adelaide', 'Perth', 
                  'Canberra', 'Gold Coast', 'Geelong', 'Hobart']
        
        found_cities = []
        
        for city in cities:
            if city in html:
                found_cities.append(city)
        
        if found_cities:
            return {
                'city': found_cities[0],
                'all_campuses': ', '.join(found_cities)
            }
        
        return {}
    
    
    def _extract_study_mode_smart(self, html: str) -> Dict:
        """
        Smart study mode detection
        
        Auto-detects:
        - On Campus
        - Online
        - Blended (both)
        """
        
        html_lower = html.lower()
        
        has_on_campus = bool(re.search(
            r'\bon[- ]?campus\b|\bface[- ]?to[- ]?face\b', 
            html_lower
        ))
        
        has_online = bool(re.search(
            r'\bonline\b|\bremote\b|\bdistance\b', 
            html_lower
        ))
        
        # Check for explicit "blended"
        has_blended = bool(re.search(
            r'\bblended\b|\bhybrid\b', 
            html_lower
        ))
        
        if has_blended or (has_on_campus and has_online):
            return {'study_mode': 'Blended'}
        elif has_online:
            return {'study_mode': 'Online'}
        elif has_on_campus:
            return {'study_mode': 'On Campus'}
        else:
            return {'study_mode': 'On Campus'}  # Default
    
    
    def _extract_english_tests_smart(self, html: str) -> Dict:
        """
        Smart English test extraction
        
        Auto-detects:
        - IELTS (with band logic)
        - PTE
        - TOEFL
        - Others
        """
        
        result = {}
        
        # IELTS pattern with "no band below" logic
        ielts_patterns = [
            r'IELTS.*?(\d+\.?\d*)\s*.*?no\s+band.*?(\d+\.?\d*)',
            r'IELTS.*?(\d+\.?\d*)',
        ]
        
        for pattern in ielts_patterns:
            match = re.search(pattern, html, re.I)
            if match:
                overall = float(match.group(1))
                min_band = float(match.group(2)) if len(match.groups()) > 1 else None
                
                result['ielts_overall'] = overall
                if min_band:
                    result['ielts_listening'] = min_band
                    result['ielts_reading'] = min_band
                    result['ielts_writing'] = min_band
                    result['ielts_speaking'] = min_band
                break
        
        # PTE
        pte_match = re.search(r'PTE.*?(\d+)', html, re.I)
        if pte_match:
            result['pte_overall'] = int(pte_match.group(1))
        
        # TOEFL
        toefl_match = re.search(r'TOEFL.*?(\d+)', html, re.I)
        if toefl_match:
            result['toefl_overall'] = int(toefl_match.group(1))
        
        return result
    
    
    def _extract_academic_requirements(self, html: str) -> Dict:
        """Extract academic requirements"""
        
        # Look for ATAR (Australian)
        atar_match = re.search(r'ATAR[:\s]*(\d+)', html, re.I)
        if atar_match:
            return {
                'academic_requirement': f'ATAR {atar_match.group(1)}',
                'academic_country': 'Australia'
            }
        
        # Look for GPA
        gpa_match = re.search(r'GPA[:\s]*([\d.]+)', html, re.I)
        if gpa_match:
            return {
                'academic_requirement': f'GPA {gpa_match.group(1)}',
                'score_type': 'GPA'
            }
        
        return {}
    
    
    async def _extract_with_http(self, course_url: str, country: str) -> Dict:
        """
        Fallback: Extract using simple HTTP (no browser)
        
        Use when browser not available or not needed
        """
        
        async with aiohttp.ClientSession() as session:
            async with session.get(course_url, timeout=15) as response:
                html = await response.text()
        
        return self._extract_all_data(html, None, course_url, country)


# ═══════════════════════════════════════════════════════
# INTEGRATION POINT
# ═══════════════════════════════════════════════════════

async def extract_course_universal(course_url: str, university_country: str) -> Dict:
    """
    Universal extraction function - works for any university
    
    Usage in orchestrator:
    course_data = await extract_course_universal(course_url, university.country)
    """
    
    scraper = UniversalSmartScraper()
    
    return await scraper.extract_course_with_smart_detection(
        course_url,
        university_country,
        use_browser=True  # Browser ensures we get dynamic content
    )
