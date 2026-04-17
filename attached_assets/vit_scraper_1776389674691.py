"""
VIT (Victorian Institute of Technology) Specific Scraper
Handles VIT's unique structure:
- Course list with category filters
- Domestic/International toggle
- Tab-based navigation for requirements
- Full course fees (not annual)
"""

import re
import asyncio
from typing import Dict, List, Optional
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import aiohttp
from playwright.async_api import async_playwright


class VITScraper:
    """
    Site-specific scraper for VIT
    
    CRITICAL VIT REQUIREMENTS:
    1. Use /course-list page (not /courses)
    2. Click "International" button to get international fees
    3. Navigate to "Entry Requirements" tab for IELTS
    4. Filter out "Key Dates" pages (junk)
    5. Recognize full course fees (not annual)
    """
    
    def __init__(self):
        self.base_url = 'https://vit.edu.au'
        
        # VIT course categories
        self.course_categories = [
            'bits',  # Bachelor of IT and Systems
            'mits',  # Master of IT and Systems
            'mba',   # MBA programs
            'bbus',  # Bachelor of Business
            'vocational',  # Vocational courses
        ]
        
        # Junk page patterns (specific to VIT)
        self.junk_patterns = [
            r'keydates?',  # "MBA Domestic Keydates", "MBA Int Keydates"
            r'key[- ]dates?',
            r'domestic[- ]keydates?',
            r'int[- ]keydates?',
            r'international[- ]keydates?',
        ]
    
    
    async def discover_all_courses(self) -> List[str]:
        """
        Discover ALL VIT courses from course-list page
        
        Returns list of course URLs
        """
        
        all_courses = []
        
        print(f"[INFO] VIT: Discovering courses from course-list page")
        
        # Try each category
        for category in self.course_categories:
            category_url = f'{self.base_url}/course-list?course_categories[0]={category}'
            
            print(f"[INFO] VIT: Fetching {category.upper()} courses from: {category_url}")
            
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(category_url, timeout=15) as response:
                        html = await response.text()
                
                # Parse course links
                soup = BeautifulSoup(html, 'html.parser')
                
                # Find all course links
                # VIT course URLs: /bits/..., /mits/..., /mba/..., /bbus/...
                for link in soup.find_all('a', href=True):
                    href = link.get('href')
                    
                    # Check if this is a course link
                    if self._is_course_link(href):
                        full_url = urljoin(self.base_url, href)
                        
                        # Filter junk
                        if not self._is_junk_page(full_url):
                            if full_url not in all_courses:
                                all_courses.append(full_url)
                
                print(f"[INFO] VIT: Found {len([u for u in all_courses if f'/{category}/' in u])} {category.upper()} courses")
                
            except Exception as e:
                print(f"[WARNING] VIT: Failed to fetch {category} courses: {e}")
                continue
        
        print(f"[SUCCESS] VIT: Discovered {len(all_courses)} total courses")
        
        return all_courses
    
    
    def _is_course_link(self, href: str) -> bool:
        """Check if URL is a VIT course page"""
        
        # VIT course URL patterns
        for category in self.course_categories:
            if f'/{category}/' in href.lower():
                return True
        
        return False
    
    
    def _is_junk_page(self, url: str) -> bool:
        """Filter junk pages (Key Dates, etc.)"""
        
        url_lower = url.lower()
        
        for pattern in self.junk_patterns:
            if re.search(pattern, url_lower):
                print(f"[FILTERED] VIT: Junk page detected: {url}")
                return True
        
        return False
    
    
    async def extract_course_data(self, course_url: str) -> Dict:
        """
        Extract complete course data with browser automation
        
        Steps:
        1. Launch browser
        2. Click "International" button
        3. Navigate to "Entry Requirements" tab
        4. Extract all data
        5. Return structured data
        """
        
        print(f"[INFO] VIT: Extracting {course_url} with browser automation")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            try:
                # ═══════════════════════════════════════════════════════
                # STEP 1: Load page
                # ═══════════════════════════════════════════════════════
                
                await page.goto(course_url, wait_until='networkidle', timeout=30000)
                
                # ═══════════════════════════════════════════════════════
                # STEP 2: Click "International" button
                # ═══════════════════════════════════════════════════════
                
                try:
                    # Wait for button to appear
                    international_button = await page.wait_for_selector(
                        'text="International"',
                        timeout=5000
                    )
                    
                    # Click it
                    await international_button.click()
                    print("[INFO] VIT: Clicked 'International' button")
                    
                    # Wait for content to update
                    await page.wait_for_timeout(2000)
                    
                except Exception as e:
                    print(f"[WARNING] VIT: Could not click International button: {e}")
                
                # Get main page HTML (with international fees)
                main_html = await page.content()
                
                # ═══════════════════════════════════════════════════════
                # STEP 3: Navigate to "Entry Requirements" tab
                # ═══════════════════════════════════════════════════════
                
                requirements_html = None
                
                try:
                    # Click "Entry Requirements" tab
                    entry_tab = await page.wait_for_selector(
                        'text="Entry Requirements"',
                        timeout=5000
                    )
                    
                    await entry_tab.click()
                    print("[INFO] VIT: Clicked 'Entry Requirements' tab")
                    
                    # Wait for tab content
                    await page.wait_for_timeout(1000)
                    
                    # Get requirements HTML
                    requirements_html = await page.content()
                    
                except Exception as e:
                    print(f"[WARNING] VIT: Could not navigate to Entry Requirements tab: {e}")
                
                await browser.close()
                
                # ═══════════════════════════════════════════════════════
                # STEP 4: Extract all data
                # ═══════════════════════════════════════════════════════
                
                course_data = {}
                
                # Extract from main page (with international view)
                course_data.update(self._extract_basic_info(main_html))
                course_data.update(self._extract_vit_fee(main_html))
                course_data.update(self._extract_duration(main_html))
                course_data.update(self._extract_intakes(main_html))
                course_data.update(self._extract_locations(main_html))
                course_data.update(self._extract_study_mode(main_html))
                
                # Extract from requirements tab
                if requirements_html:
                    course_data.update(self._extract_english_requirements(requirements_html))
                
                course_data['course_url'] = course_url
                
                return course_data
                
            except Exception as e:
                print(f"[ERROR] VIT: Extraction failed for {course_url}: {e}")
                await browser.close()
                return {'course_url': course_url, 'error': str(e)}
    
    
    def _extract_basic_info(self, html: str) -> Dict:
        """Extract course name and description"""
        
        soup = BeautifulSoup(html, 'html.parser')
        
        # Course name (h1)
        title_tag = soup.find('h1')
        course_name = title_tag.get_text(strip=True) if title_tag else None
        
        # Description (first paragraph)
        description = None
        first_p = soup.find('p')
        if first_p:
            description = first_p.get_text(strip=True)
        
        return {
            'course_name': course_name,
            'course_description': description
        }
    
    
    def _extract_vit_fee(self, html: str) -> Dict:
        """
        Extract VIT fee (CRITICAL: Full course, not annual)
        
        VIT fee format:
        - "$48,000 ($3,000/unit)"
        - "$51,000 ($2,125/unit) - 24 Units"
        
        This is FULL COURSE fee, not annual
        """
        
        # Pattern 1: Full course fee with unit breakdown
        pattern1 = r'\$?([\d,]+)\s*\(\$?([\d,]+)/unit\)'
        match = re.search(pattern1, html)
        
        if match:
            full_course_fee = float(match.group(1).replace(',', ''))
            
            print(f"[INFO] VIT: Extracted fee: ${full_course_fee:,.0f} (Full Course)")
            
            return {
                'international_fee': full_course_fee,
                'fee_term': 'Full Course',
                'fee_year': 2026,
                'currency': 'AUD'
            }
        
        # Pattern 2: Just fee amount
        pattern2 = r'Fees[:\s]*\$?([\d,]+)'
        match = re.search(pattern2, html, re.I)
        
        if match:
            fee = float(match.group(1).replace(',', ''))
            
            return {
                'international_fee': fee,
                'fee_term': 'Full Course',  # VIT always shows full course
                'fee_year': 2026,
                'currency': 'AUD'
            }
        
        return {}
    
    
    def _extract_duration(self, html: str) -> Dict:
        """
        Extract duration
        
        VIT format:
        - "3 Years (Full-Time)/2 Years (Fast-Track)"
        - "2 Years (Full-Time)"
        """
        
        # Pattern: "2 Years (Full-Time)"
        pattern = r'(\d+(?:\.\d+)?)\s*Years?\s*\(Full[- ]Time\)'
        match = re.search(pattern, html, re.I)
        
        if match:
            duration = float(match.group(1))
            
            return {
                'duration': duration,
                'duration_term': 'Years'
            }
        
        return {}
    
    
    def _extract_intakes(self, html: str) -> Dict:
        """
        Extract intakes
        
        VIT format:
        - "02-Mar-2026"
        - "25-May-2026"
        - Multiple dates listed
        """
        
        # Pattern: date format "02-Mar-2026"
        date_pattern = r'\d{2}-([A-Z][a-z]+)-\d{4}'
        matches = re.findall(date_pattern, html)
        
        if matches:
            # Get unique months
            months = list(set(matches))
            
            return {
                'intake_months': ', '.join(months),
                'intake_month': months[0] if months else None
            }
        
        return {}
    
    
    def _extract_locations(self, html: str) -> Dict:
        """
        Extract campus locations
        
        VIT has multiple campuses: Melbourne, Sydney, Adelaide, Geelong
        """
        
        cities = []
        
        # Common VIT cities
        vit_cities = ['Melbourne', 'Sydney', 'Adelaide', 'Geelong']
        
        for city in vit_cities:
            if city in html:
                cities.append(city)
        
        if cities:
            return {
                'city': cities[0],  # Primary campus
                'all_campuses': ', '.join(cities)
            }
        
        return {}
    
    
    def _extract_study_mode(self, html: str) -> Dict:
        """
        Extract study mode
        
        VIT shows:
        - "On-campus"
        - "Online"
        - Both = Blended
        """
        
        has_on_campus = bool(re.search(r'on[- ]campus', html, re.I))
        has_online = bool(re.search(r'\bonline\b', html, re.I))
        
        if has_on_campus and has_online:
            study_mode = 'Blended'
        elif has_online:
            study_mode = 'Online'
        elif has_on_campus:
            study_mode = 'On Campus'
        else:
            study_mode = 'On Campus'  # Default for VIT
        
        return {'study_mode': study_mode}
    
    
    def _extract_english_requirements(self, html: str) -> Dict:
        """
        Extract IELTS from Entry Requirements tab
        
        VIT format:
        "IELTS Academic overall score of 6.5, with no band below 6.0"
        """
        
        # Pattern: "IELTS Academic overall score of 6.5, with no band below 6.0"
        pattern = r'IELTS.*?overall\s+score\s+of\s+([\d.]+).*?no\s+band\s+below\s+([\d.]+)'
        match = re.search(pattern, html, re.I)
        
        if match:
            overall = float(match.group(1))
            min_band = float(match.group(2))
            
            print(f"[INFO] VIT: Extracted IELTS: {overall} (min {min_band})")
            
            return {
                'ielts_overall': overall,
                'ielts_listening': min_band,
                'ielts_reading': min_band,
                'ielts_writing': min_band,
                'ielts_speaking': min_band
            }
        
        return {}


# ═══════════════════════════════════════════════════════
# INTEGRATION WITH MAIN SCRAPER
# ═══════════════════════════════════════════════════════

async def scrape_vit_university(university_id: int) -> Dict:
    """
    Main entry point for VIT scraping
    
    Usage:
    result = await scrape_vit_university(university_id=16)
    """
    
    vit = VITScraper()
    
    # ═══════════════════════════════════════════════════════
    # PHASE 1: Discover all courses
    # ═══════════════════════════════════════════════════════
    
    all_course_urls = await vit.discover_all_courses()
    
    print(f"[INFO] VIT: Total courses discovered: {len(all_course_urls)}")
    
    # ═══════════════════════════════════════════════════════
    # PHASE 2: Extract data from each course
    # ═══════════════════════════════════════════════════════
    
    extracted_courses = []
    
    for idx, course_url in enumerate(all_course_urls):
        print(f"[{idx+1}/{len(all_course_urls)}] VIT: Extracting {course_url}")
        
        course_data = await vit.extract_course_data(course_url)
        
        if course_data and not course_data.get('error'):
            extracted_courses.append(course_data)
        
        # Small delay
        await asyncio.sleep(1)
    
    # ═══════════════════════════════════════════════════════
    # PHASE 3: Return results
    # ═══════════════════════════════════════════════════════
    
    return {
        'university_id': university_id,
        'courses_discovered': len(all_course_urls),
        'courses_extracted': len(extracted_courses),
        'courses': extracted_courses
    }


# ═══════════════════════════════════════════════════════
# TESTING
# ═══════════════════════════════════════════════════════

if __name__ == '__main__':
    # Test VIT scraper
    import asyncio
    
    async def test():
        result = await scrape_vit_university(university_id=16)
        
        print(f"\n{'='*60}")
        print(f"VIT SCRAPING RESULTS")
        print(f"{'='*60}")
        print(f"Courses discovered: {result['courses_discovered']}")
        print(f"Courses extracted: {result['courses_extracted']}")
        print(f"\nSample courses:")
        
        for course in result['courses'][:5]:
            print(f"\n  - {course.get('course_name')}")
            print(f"    Fee: ${course.get('international_fee'):,.0f} ({course.get('fee_term')})")
            print(f"    Duration: {course.get('duration')} {course.get('duration_term')}")
            print(f"    IELTS: {course.get('ielts_overall')}")
    
    asyncio.run(test())
