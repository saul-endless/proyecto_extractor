# -*- coding: utf-8 -*-
"""
Funciones especializadas de extracción de campos.
Resuelve problemas de campos vacíos y truncados.
"""

import re
from datetime import datetime
from difflib import SequenceMatcher


# ==================== FECHAS ====================

MONTH_MAP_ES = {
    'ENE': '01', 'ENERO': '01',
    'FEB': '02', 'FEBRERO': '02',
    'MAR': '03', 'MARZO': '03',
    'ABR': '04', 'ABRIL': '04',
    'MAY': '05', 'MAYO': '05',
    'JUN': '06', 'JUNIO': '06',
    'JUL': '07', 'JULIO': '07',
    'AGO': '08', 'AGOSTO': '08',
    'SEP': '09', 'SEPTIEMBRE': '09',
    'OCT': '10', 'OCTUBRE': '10',
    'NOV': '11', 'NOVIEMBRE': '11',
    'DIC': '12', 'DICIEMBRE': '12'
}

DATE_PATTERNS = [
    r'(\d{1,2})/([A-Z]{3})/(\d{4})',
    r'(\d{1,2})-([A-Z]{3})-(\d{4})',
    r'(\d{1,2})/(\d{2})/(\d{4})',
    r'(\d{1,2})-(\d{2})-(\d{4})',
]


def extract_and_normalize_date(text, period_start=None, period_end=None):
    """Extrae y normaliza fechas a formato DD-MMM-YYYY."""
    text_upper = text.upper()
    
    for pattern in DATE_PATTERNS:
        match = re.search(pattern, text_upper)
        if match:
            day, month, year = match.groups()
            
            if month.upper() in MONTH_MAP_ES:
                month_num = MONTH_MAP_ES[month.upper()]
            elif month.isdigit():
                month_num = month.zfill(2)
            else:
                continue
            
            try:
                date_obj = datetime(int(year), int(month_num), int(day))
                
                if period_start and period_end:
                    if not (period_start <= date_obj <= period_end):
                        continue
                
                month_abbr_es = list(MONTH_MAP_ES.keys())[int(month_num) - 1]
                if len(month_abbr_es) > 3:
                    month_abbr_es = month_abbr_es[:3]
                
                return f"{day.zfill(2)}-{month_abbr_es}-{year}"
            
            except ValueError:
                continue
    
    return None


# ==================== MONTOS ====================

def extract_amount(text, default_sign=1):
    """Extrae y normaliza montos monetarios."""
    text = str(text).strip()
    
    sign = -1 if '(' in text or ')' in text else default_sign
    text = text.replace('(', '').replace(')', '')
    
    text = re.sub(r'[\$€£¥]|MXN|USD|EUR', '', text, flags=re.IGNORECASE)
    text = text.replace(' ', '')
    
    parts = re.findall(r'[\d,\.]+', text)
    if not parts:
        return None
    
    amount_str = parts[0]
    
    if ',' in amount_str and '.' in amount_str:
        comma_pos = amount_str.rfind(',')
        dot_pos = amount_str.rfind('.')
        
        if comma_pos > dot_pos:
            amount_str = amount_str.replace('.', '').replace(',', '.')
        else:
            amount_str = amount_str.replace(',', '')
    
    elif ',' in amount_str:
        comma_pos = amount_str.rfind(',')
        digits_after = len(amount_str) - comma_pos - 1
        
        if digits_after == 2:
            amount_str = amount_str.replace(',', '.')
        else:
            amount_str = amount_str.replace(',', '')
    
    try:
        amount = float(amount_str) * sign
        return round(amount, 2)
    except ValueError:
        return None


# ==================== CUENTAS ====================

def extract_account_number(text, prefer_clabe=False):
    """Extrae numero de cuenta o CLABE."""
    text_clean = str(text).replace('-', '').replace(' ', '').replace('.', '')
    
    sequences = re.findall(r'\d{8,18}', text_clean)
    
    if not sequences:
        return ""
    
    accounts_10 = [s for s in sequences if len(s) == 10]
    accounts_18 = [s for s in sequences if len(s) == 18]
    
    if prefer_clabe and accounts_18:
        return accounts_18[0]
    elif accounts_10:
        return accounts_10[0]
    elif accounts_18:
        return accounts_18[0]
    else:
        return sequences[0] if sequences else ""


# ==================== REFERENCIAS ====================

def extract_reference(text):
    """Extrae numero de referencia o folio."""
    patterns = [
        r'REF[\.:]\s*([A-Z0-9]{4,20})',
        r'REFERENCIA[\.:]\s*([A-Z0-9]{4,20})',
        r'FOLIO[\.:]\s*([A-Z0-9]{4,20})',
        r'Ref[\.:]\s*([A-Z0-9]{4,20})',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text.upper())
        if match:
            return match.group(1)
    
    sequences = re.findall(r'\b[A-Z0-9]{7,15}\b', text.upper())
    return sequences[0] if sequences else ""


# ==================== NOMBRES COMPLETOS ====================

def extract_full_transaction_name(transaction_lines):
    """
    Extrae el nombre COMPLETO de la columna COD. DESCRIPCIÓN.
    Elimina montos y saldos, une todo con espacios.
    """
    if not transaction_lines:
        return ""
    
    full_name_parts = []
    
    for line in transaction_lines:
        line_clean = line.strip()
        
        # Parar si encontramos una nueva transacción (nueva fecha)
        if re.match(r'\d{2}/[A-Z]{3}', line_clean.upper()) and full_name_parts:
            break
        
        # Parar si es separador
        if re.match(r'^[\s\-=_]+$', line_clean):
            break
        
        # Parar si encontramos pie de página
        if any(keyword in line_clean.upper() for keyword in [
            'INFORMACION FINANCIERA',
            'ESTADO DE CUENTA',
            'PAGINA',
            'BBVA MEXICO, S.A.'
        ]):
            break
        
        if not line_clean:
            continue
        
        # Eliminar montos al final de la línea
        cleaned_line = re.sub(r'(\s+[\d,]+\.\d{2})+\s*$', '', line_clean)
        
        if cleaned_line:
            full_name_parts.append(cleaned_line)
    
    # UNIR CON ESPACIOS (no saltos de línea)
    return ' '.join(full_name_parts)


# ==================== BENEFICIARIOS/ORDENANTES ====================

def extract_beneficiary_name(transaction_lines):
    """Extrae el nombre del beneficiario/ordenante."""
    name_pattern = r'^([A-Z][A-Z\s\.]{10,80})$'
    
    for line in transaction_lines[1:]:
        line_clean = line.strip()
        
        if re.search(r'\d{10,}', line_clean):
            continue
        
        if any(keyword in line_clean.upper() for keyword in [
            'REF', 'BNET', 'AUT:', 'TC0', 'USD', 'RFC:', 'CIE:'
        ]):
            continue
        
        match = re.match(name_pattern, line_clean)
        if match:
            name = match.group(1).strip()
            
            words = name.split()
            alpha_words = [w for w in words if w.replace('.', '').isalpha()]
            
            if 2 <= len(words) <= 6 and len(alpha_words) >= len(words) * 0.7:
                return name
    
    return ""


# ==================== NOMBRE RESUMIDO ====================

def create_summarized_name(full_name, transaction_type, method):
    """Crea un nombre resumido estandarizado."""
    action_keywords = {
        'SPEI RECIBIDO': 'SPEI RECIBIDO desde',
        'SPEI ENVIADO': 'SPEI ENVIADO a',
        'DEPOSITO': 'DEPOSITO de',
        'PAGO': 'PAGO a',
        'TRANSFERENCIA': 'TRANSFERENCIA a',
        'CARGO': 'CARGO de',
        'COMISION': 'COMISION'
    }
    
    action = None
    for keyword, template in action_keywords.items():
        if keyword in full_name.upper():
            action = template
            break
    
    if not action:
        action = method or transaction_type or ""
    
    bank_pattern = r'\b(BANORTE|HSBC|INBURSA|AZTECA|SANTANDER|BANAMEX|SCOTIABANK|BANREGIO|STP|BANCOMER|BBVA)\b'
    bank_match = re.search(bank_pattern, full_name.upper())
    
    if bank_match:
        entity = bank_match.group(1)
        summary = f"{action} {entity}"
    else:
        name_match = re.search(r'\b([A-Z][A-Z\s]{10,50})\b', full_name)
        if name_match:
            entity = name_match.group(1).strip()[:30]
            summary = f"{action} {entity}"
        else:
            words = full_name.split()[:8]
            summary = ' '.join(words)
    
    return summary[:60].strip()


# ==================== SUCURSAL ====================

def extract_branch_from_header(pdf_text):
    """Extrae la sucursal del encabezado del estado de cuenta."""
    pattern = r'SUCURSAL\s*:\s*(\d{4})'
    match = re.search(pattern, pdf_text.upper())
    
    return match.group(1) if match else ""


# ==================== CLASIFICACION ====================

TRANSACTION_TYPE_MAP = {
    'T20': ('Transferencia', 'SPEI'),
    'T17': ('Transferencia', 'SPEI'),
    'W02': ('Deposito', 'Deposito de tercero'),
    'W01': ('Deposito', 'Efectivo'),
    'A15': ('Pago', 'Tarjeta'),
    'A14': ('Pago', 'Tarjeta'),
    'S39': ('Cargo por servicio', 'Comision'),
    'S40': ('Cargo por servicio', 'IVA comision'),
    'G30': ('Cargo por servicio', 'Comision'),
    'N06': ('Pago', 'Pago cuenta tercero'),
    'P14': ('Pago', 'Pago SAT'),
}


def classify_transaction(code, description, amount_column):
    """Clasifica la transaccion en Tipo, Metodo y si es Ingreso/Egreso."""
    if code in TRANSACTION_TYPE_MAP:
        tipo, metodo = TRANSACTION_TYPE_MAP[code]
    else:
        desc_upper = description.upper()
        if 'SPEI' in desc_upper:
            tipo, metodo = 'Transferencia', 'SPEI'
        elif 'DEPOSITO' in desc_upper or 'ABONO' in desc_upper:
            tipo, metodo = 'Deposito', 'Efectivo'
        elif 'PAGO' in desc_upper:
            tipo, metodo = 'Pago', 'Transferencia'
        elif 'TARJETA' in desc_upper or 'POS' in desc_upper:
            tipo, metodo = 'Pago', 'Tarjeta'
        elif 'COMISION' in desc_upper or 'IVA' in desc_upper:
            tipo, metodo = 'Cargo por servicio', 'Comision'
        elif 'CHEQUE' in desc_upper:
            tipo, metodo = 'Retiro', 'Cheque'
        else:
            tipo, metodo = 'Otro', 'Otro'
    
    if amount_column == 'ABONOS':
        clasificacion = 'Ingreso'
    elif amount_column == 'CARGOS':
        clasificacion = 'Egreso'
    else:
        clasificacion = 'Ingreso' if tipo in ['Deposito'] else 'Egreso'
    
    return tipo, metodo, clasificacion