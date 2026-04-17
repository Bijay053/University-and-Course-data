"""
Data Validator - Production Ready
Validates extracted course data before staging
"""

from typing import Dict, List


class DataValidator:
    """
    Validate extracted course data before staging
    """
    
    def validate_course(self, course_data: Dict) -> Dict:
        """
        Validate course data and return validation report
        
        Returns:
        {
            'score': float (0.0 - 1.0),
            'missing_fields': list,
            'warnings': list,
            'errors': list
        }
        """
        
        missing = []
        warnings = []
        errors = []
        
        # ─────────────────────────────────────────────────────
        # CHECK REQUIRED FIELDS
        # ─────────────────────────────────────────────────────
        
        required_fields = [
            'course_name',
            'degree_level',
            'duration',
            'duration_term',
            'study_mode'
        ]
        
        for field in required_fields:
            if not course_data.get(field):
                missing.append(field)
        
        # ─────────────────────────────────────────────────────
        # CHECK IMPORTANT FIELDS
        # ─────────────────────────────────────────────────────
        
        important_fields = [
            'international_fee',
            'ielts_overall',
            'intake_month'
        ]
        
        for field in important_fields:
            if not course_data.get(field):
                warnings.append(f'Missing {field}')
        
        # ─────────────────────────────────────────────────────
        # VALIDATE DURATION
        # ─────────────────────────────────────────────────────
        
        duration = course_data.get('duration')
        term = course_data.get('duration_term')
        
        if duration and term:
            # Convert to years
            if term == 'Months':
                years = duration / 12
            elif term == 'Years':
                years = duration
            elif term == 'Semesters':
                years = duration / 2
            elif term == 'Trimesters':
                years = duration / 3
            else:
                years = duration
            
            # Check realistic range
            if years > 10:
                errors.append(f'Unrealistic duration: {duration} {term}')
            elif years < 0.25:
                errors.append(f'Duration too short: {duration} {term}')
        
        # ─────────────────────────────────────────────────────
        # VALIDATE FEE
        # ─────────────────────────────────────────────────────
        
        fee = course_data.get('international_fee')
        
        if fee:
            if fee < 1000 or fee > 100000:
                warnings.append(f'Unusual fee amount: {fee}')
        
        # ─────────────────────────────────────────────────────
        # VALIDATE IELTS SCORES
        # ─────────────────────────────────────────────────────
        
        ielts_overall = course_data.get('ielts_overall')
        
        if ielts_overall:
            if ielts_overall < 4.0 or ielts_overall > 9.0:
                errors.append(f'Invalid IELTS score: {ielts_overall}')
        
        # ─────────────────────────────────────────────────────
        # CALCULATE SCORE
        # ─────────────────────────────────────────────────────
        
        total_fields = len(required_fields) + len(important_fields)
        present_fields = (
            len([f for f in required_fields if course_data.get(f)]) +
            len([f for f in important_fields if course_data.get(f)])
        )
        
        score = present_fields / total_fields
        
        # Penalize for errors
        if errors:
            score *= 0.5
        
        return {
            'score': round(score, 2),
            'missing_fields': missing,
            'warnings': warnings,
            'errors': errors
        }
