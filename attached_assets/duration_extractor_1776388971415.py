"""
Duration & Study Mode Extractor - Production Ready
Extracts duration, term, and study mode with validation
"""

import re
from typing import Dict, Optional
from bs4 import BeautifulSoup


class DurationExtractor:
    """
    Extract duration, term, and study mode with VALIDATION
    
    Prevents errors like:
    - "21 Year" durations
    - Missing "Blended" when both online and on-campus offered
    """
    
    async def extract(self, html: str, url: str, country: str) -> Dict:
        """
        Extract duration with validation
        """
        
        text = re.sub(r'<[^>]+>', ' ', html)
        
        # Extract duration
        duration_data = self._extract_duration(text)
        
        # Extract study mode (needs HTML for Location field parsing)
        study_mode = self._extract_study_mode(text, html)
        
        # Validate duration
        validated = self._validate_duration(duration_data)
        
        return {
            'duration': validated['duration'],
            'duration_term': validated['term'],
            'study_mode': study_mode
        }
    
    
    def _extract_duration(self, text: str) -> Dict:
        """Extract duration with multiple patterns"""
        
        # Pattern 1: "2 years" or "18 months"
        pattern1 = r'(\d+(?:\.\d+)?)\s+(years?|months?|weeks?|semesters?|trimesters?)'
        match = re.search(pattern1, text, re.I)
        if match:
            return {
                'duration': float(match.group(1)),
                'term': self._normalize_term(match.group(2))
            }
        
        # Pattern 2: "Duration: 1.5 years"
        pattern2 = r'duration[:\s]+(\d+(?:\.\d+)?)\s+(years?|months?)'
        match = re.search(pattern2, text, re.I)
        if match:
            return {
                'duration': float(match.group(1)),
                'term': self._normalize_term(match.group(2))
            }
        
        # Pattern 3: "6 trimesters"
        pattern3 = r'(\d+)\s+(semesters?|trimesters?)'
        match = re.search(pattern3, text, re.I)
        if match:
            return {
                'duration': int(match.group(1)),
                'term': self._normalize_term(match.group(2))
            }
        
        return {'duration': None, 'term': None}
    
    
    def _normalize_term(self, term: str) -> str:
        """Normalize term to standard values"""
        
        term_lower = term.lower().rstrip('s')
        
        mapping = {
            'year': 'Years',
            'month': 'Months',
            'week': 'Weeks',
            'semester': 'Semesters',
            'trimester': 'Trimesters'
        }
        
        return mapping.get(term_lower, 'Years')
    
    
    def _validate_duration(self, duration_data: Dict) -> Dict:
        """
        CRITICAL: Validate duration to prevent errors like "21 Year"
        
        Rules:
        - Graduate Certificate: 6 months - 1 year
        - Graduate Diploma: 1 - 2 years
        - Bachelor: 3 - 5 years
        - Master: 1 - 3 years
        - PhD: 3 - 5 years
        - Maximum: 10 years (anything above is likely wrong)
        - Minimum: 3 months
        """
        
        duration = duration_data.get('duration')
        term = duration_data.get('term')
        
        if not duration or not term:
            return {'duration': None, 'term': None}
        
        # Convert to years for validation
        duration_in_years = duration
        
        if term == 'Months':
            duration_in_years = duration / 12
        elif term == 'Weeks':
            duration_in_years = duration / 52
        elif term == 'Semesters':
            duration_in_years = duration / 2
        elif term == 'Trimesters':
            duration_in_years = duration / 3
        
        # VALIDATION: Reject unrealistic durations
        if duration_in_years > 10:
            print(f"[WARNING] Duration {duration} {term} ({duration_in_years:.1f} years) is unrealistic - rejecting")
            return {'duration': None, 'term': None}
        
        if duration_in_years < 0.25:  # Less than 3 months
            print(f"[WARNING] Duration {duration} {term} ({duration_in_years:.1f} years) is too short - rejecting")
            return {'duration': None, 'term': None}
        
        return {
            'duration': duration,
            'term': term
        }
    
    
    def _extract_study_mode(self, text: str, html: str) -> str:
        """
        Extract study mode with proper blended detection
        
        CRITICAL: Detect when both on-campus AND online are offered
        
        Examples:
        - "Location: Sydney, Online" = Blended
        - "Delivery: Face to Face on campus" + "Online" somewhere = Blended
        - "Online only" = Online
        - "On campus only" = On Campus
        """
        
        text_lower = text.lower()
        
        # Parse HTML for structured fields
        soup = BeautifulSoup(html, 'html.parser')
        
        has_online = False
        has_on_campus = False
        
        # ─────────────────────────────────────────────────────
        # CHECK 1: Location field (ASAHE/Torrens pattern)
        # ─────────────────────────────────────────────────────
        
        location_indicators = soup.find_all(string=re.compile(r'location', re.I))
        
        for indicator in location_indicators:
            parent = indicator.find_parent(['div', 'section', 'td', 'li', 'dt', 'dd'])
            if parent:
                parent_text = parent.get_text().lower()
                
                if 'online' in parent_text:
                    has_online = True
                
                # City names indicate on-campus
                if any(city in parent_text for city in [
                    'sydney', 'melbourne', 'brisbane', 'adelaide', 'perth',
                    'auckland', 'wellington', 'toronto', 'vancouver',
                    'london', 'manchester', 'campus'
                ]):
                    has_on_campus = True
        
        # ─────────────────────────────────────────────────────
        # CHECK 2: Delivery/Mode field
        # ─────────────────────────────────────────────────────
        
        delivery_indicators = soup.find_all(string=re.compile(r'delivery|mode', re.I))
        
        for indicator in delivery_indicators:
            parent = indicator.find_parent(['div', 'section', 'td', 'li', 'dt', 'dd'])
            if parent:
                parent_text = parent.get_text().lower()
                
                if any(kw in parent_text for kw in [
                    'face to face', 'on campus', 'on-campus', 'in person'
                ]):
                    has_on_campus = True
                
                if 'online' in parent_text:
                    has_online = True
        
        # ─────────────────────────────────────────────────────
        # CHECK 3: General text patterns
        # ─────────────────────────────────────────────────────
        
        if re.search(r'\bon[- ]?campus\b|\bface[- ]?to[- ]?face\b|\bin[- ]?person\b', text_lower):
            has_on_campus = True
        
        if re.search(r'\bonline\b|\bremote\b|\bdistance\b|\bexternal\b', text_lower):
            has_online = True
        
        # Explicit blended/hybrid
        if re.search(r'\bblended\b|\bhybrid\b|\bmixed\s+mode\b', text_lower):
            return 'Blended'
        
        # Explicit "both"
        if re.search(r'\bboth\b.*\bonline\b.*\bcampus\b|\bboth\b.*\bcampus\b.*\bonline\b', text_lower):
            return 'Blended'
        
        # ─────────────────────────────────────────────────────
        # DETERMINE FINAL MODE
        # ─────────────────────────────────────────────────────
        
        if has_online and has_on_campus:
            return 'Blended'
        elif has_online:
            return 'Online'
        elif has_on_campus:
            return 'On Campus'
        else:
            # Default to On Campus if not specified
            return 'On Campus'
