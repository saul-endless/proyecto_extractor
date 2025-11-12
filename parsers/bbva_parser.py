# -*- coding: utf-8 -*-
"""
Parser BBVA Empresa v3.3 FINAL
Corrige: montos de transacciones y metadatos
"""

import re
from datetime import datetime
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.field_extractors import (
    extract_and_normalize_date,
    extract_amount,
    extract_account_number,
    extract_reference,
    extract_full_transaction_name,
    extract_beneficiary_name,
    create_summarized_name,
    extract_branch_from_header,
    classify_transaction
)


def parse_bbva_empresa(text_data, ocr_data=None):
    """Parser principal BBVA Empresa."""
    print("\n=== Iniciando Parser BBVA Empresa v2.0 ===")
    
    metadata = extract_metadata(text_data)
    print(f"✓ Metadatos extraídos: {metadata['nombre_empresa']}")
    print(f"✓ Período: {metadata['periodo']}")
    print(f"✓ Sucursal extraída: {metadata.get('sucursal', 'N/A')}")
    
    transactions = extract_transactions(text_data, metadata)
    print(f"✓ Transacciones extraídas: {len(transactions)}")
    
    validate_balance(transactions, metadata)
    
    return {
        'metadata': metadata,
        'transactions': transactions
    }


def extract_metadata(text):
    """Extrae metadatos."""
    metadata = {
        'nombre_empresa': '',
        'periodo': '',
        'numero_cuenta_clabe': '',
        'numero_cuenta': '',
        'saldo_inicial': '0',
        'total_depositos': '0',
        'total_retiros': '0',
        'saldo_final': '0',
        'saldo_promedio': '0',
        'sucursal': '',
        'rfc': '',
        'numero_cliente': ''
    }
    
    lines = text.split('\n')
    
    # Nombre de empresa
    for line in lines[:30]:
        if re.search(r'(TECHNOLOGIES|SA DE CV)', line, re.IGNORECASE):
            metadata['nombre_empresa'] = line.strip()
            break
    
    # Período
    period_match = re.search(r'DEL\s+(\d{2}/\d{2}/\d{4})\s+AL\s+(\d{2}/\d{2}/\d{4})', text, re.IGNORECASE)
    if period_match:
        metadata['periodo'] = f"DEL {period_match.group(1)} AL {period_match.group(2)}"
    
    # Cuenta
    account_match = re.search(r'No\.\s*de\s*Cuenta\s+(\d{10})', text, re.IGNORECASE)
    if account_match:
        metadata['numero_cuenta'] = account_match.group(1)
    
    # CLABE
    clabe_match = re.search(r'CLABE\s+(\d{18})', text, re.IGNORECASE)
    if clabe_match:
        metadata['numero_cuenta_clabe'] = clabe_match.group(1)
    
    # RFC
    rfc_match = re.search(r'R\.F\.C\s+([A-Z]{3,4}\d{6}[A-Z0-9]{3})', text, re.IGNORECASE)
    if rfc_match:
        metadata['rfc'] = rfc_match.group(1)
    
    # Cliente
    client_match = re.search(r'No\.\s*de\s*Cliente\s+(D\d{7})', text, re.IGNORECASE)
    if client_match:
        metadata['numero_cliente'] = client_match.group(1)
    
    # Sucursal
    branch_match = re.search(r'SUCURSAL\s*:\s*(\d{4})', text, re.IGNORECASE)
    if branch_match:
        metadata['sucursal'] = branch_match.group(1)
    
    # CRÍTICO: Extraer montos DEFINITIVO
    extract_financial_data_final(text, metadata)
    
    return metadata


def extract_financial_data_final(text, metadata):
    """
    VERSIÓN FINAL: Extrae montos usando búsqueda robusta.
    Busca el PRIMER monto > $1000 después del título.
    """
    # Buscar sección de comportamiento
    behavior_match = re.search(r'Comportamiento', text, re.IGNORECASE)
    if not behavior_match:
        return
    
    # Trabajar desde la sección de comportamiento
    behavior_section = text[behavior_match.start():]
    lines = behavior_section.split('\n')[:50]  # Primeras 50 líneas
    
    # Saldo Inicial
    for i, line in enumerate(lines):
        if 'SALDO' in line.upper() and 'LIQUIDACI' in line.upper() and 'INICIAL' in line.upper():
            # Buscar monto en las siguientes 3 líneas
            for j in range(i+1, min(i+4, len(lines))):
                amount = extract_amount(lines[j])
                if amount and 0 < amount < 1000000000:
                    metadata['saldo_inicial'] = str(amount)
                    break
            break
    
    # Depósitos / Abonos - BÚSQUEDA ROBUSTA
    for i, line in enumerate(lines):
        if 'DEP' in line.upper() and 'ABONO' in line.upper() and '(+)' in line.upper():
            # Buscar el PRIMER monto > $1000 en las siguientes 5 líneas
            for j in range(i+1, min(i+6, len(lines))):
                amount = extract_amount(lines[j])
                # Debe ser > $1000 y < $10B
                if amount and 1000 < amount < 10000000000:
                    metadata['total_depositos'] = str(amount)
                    break
            break
    
    # Retiros / Cargos - BÚSQUEDA ROBUSTA
    for i, line in enumerate(lines):
        if 'RETIRO' in line.upper() and 'CARGO' in line.upper() and '(-)' in line.upper():
            # Buscar el PRIMER monto > $1000 en las siguientes 5 líneas
            for j in range(i+1, min(i+6, len(lines))):
                amount = extract_amount(lines[j])
                if amount and 1000 < amount < 10000000000:
                    metadata['total_retiros'] = str(amount)
                    break
            break
    
    # Saldo Final
    for i, line in enumerate(lines):
        if 'SALDO FINAL' in line.upper() and '(+)' in line.upper():
            for j in range(i+1, min(i+4, len(lines))):
                amount = extract_amount(lines[j])
                if amount and 0 < amount < 1000000000:
                    metadata['saldo_final'] = str(amount)
                    break
            break


def extract_transactions(text, metadata):
    """Extrae transacciones."""
    movement_start = find_movements_section(text)
    
    if movement_start == -1:
        print("⚠️ ADVERTENCIA: No se encontró 'Detalle de Movimientos'")
        return []
    
    movement_end = text.find('Total de Movimientos', movement_start)
    if movement_end == -1:
        movement_section = text[movement_start:]
    else:
        movement_section = text[movement_start:movement_end]
    
    lines = movement_section.split('\n')
    transaction_groups = group_transaction_lines_fixed(lines)
    print(f"✓ Transacciones agrupadas: {len(transaction_groups)}")
    
    transactions = []
    sucursal_default = metadata.get('sucursal', '')
    period_start, period_end = parse_period(metadata.get('periodo', ''))
    
    for group in transaction_groups:
        transaction = parse_single_transaction_final(group, sucursal_default, period_start, period_end)
        if transaction:
            transactions.append(transaction)
    
    print(f"✓ Transacciones parseadas exitosamente: {len(transactions)}")
    return transactions


def find_movements_section(text):
    """Encuentra sección de movimientos."""
    patterns = [
        r'DETALLE\s+DE\s+MOVIMIENTOS',
        r'Detalle\s+de\s+Movimientos',
        r'FECHA\s+OPER\s+LIQ'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.start()
    
    return -1


def group_transaction_lines_fixed(lines):
    """Agrupa líneas SIN capturar pie de página."""
    groups = []
    current_group = []
    
    date_pattern = r'^\s*\d{2}/[A-Z]{3}'
    stop_patterns = [
        'INFORMACI[OÓ]N FINANCIERA',
        'ESTADO DE CUENTA',
        'PAGINA',
        'MAESTRA PYME',
        'DOMICILIO FISCAL',
        'MONEDA NACIONAL'
    ]
    
    for line in lines:
        if not line.strip():
            continue
        
        line_upper = line.upper()
        
        if any(re.search(p, line_upper) for p in stop_patterns):
            if current_group:
                groups.append(current_group)
                current_group = []
            continue
        
        if any(kw in line_upper for kw in ['FECHA', 'OPER', 'LIQ', 'DESCRIPCION', 'CARGOS', 'ABONOS']):
            if 'COD' in line_upper:
                continue
        
        if re.match(r'^[\s\-=_]+$', line):
            continue
        
        if re.match(date_pattern, line_upper):
            if current_group:
                groups.append(current_group)
            current_group = [line]
        else:
            if current_group:
                current_group.append(line)
    
    if current_group:
        groups.append(current_group)
    
    return groups


def parse_single_transaction_final(lines, sucursal_default, period_start, period_end):
    """VERSIÓN CORREGIDA: Fechas y montos."""
    if not lines:
        return None
    
    main_line = lines[0]
    full_text = '\n'.join(lines)
    
    # Fecha CON AÑO CORRECTO
    fecha_match = re.search(r'(\d{2}/[A-Z]{3})', main_line.upper())
    if not fecha_match:
        return None
    
    fecha_str = fecha_match.group(1)
    
    # CRÍTICO: Usar año del período
    if period_start:
        year = period_start.year
        # Convertir mes de 3 letras a número
        month_abbr = fecha_str.split('/')[1]
        month_num = {
            'ENE': '01', 'FEB': '02', 'MAR': '03', 'ABR': '04',
            'MAY': '05', 'JUN': '06', 'JUL': '07', 'AGO': '08',
            'SEP': '09', 'OCT': '10', 'NOV': '11', 'DIC': '12'
        }.get(month_abbr, '01')
        
        day = fecha_str.split('/')[0]
        fecha = f"{day.zfill(2)}-{month_abbr}-{year}"
    else:
        fecha = None
    
    # Código
    code_match = re.search(r'\b([A-Z]\d{2,3})\b', main_line.upper())
    code = code_match.group(1) if code_match else ""
    
    # Nombre completo
    nombre_completo = extract_full_transaction_name(lines)
    
    # Beneficiario
    quien = extract_beneficiary_name(lines)
    
    # MONTO MEJORADO
    monto = extract_transaction_amount_improved(lines, nombre_completo)
    
    if monto is None or monto <= 0:
        return None
    
    # Columna
    if code in ['T17', 'A15', 'A14', 'S39', 'S40', 'G30', 'P14', 'N06']:
        amount_column = 'CARGOS'
    elif code in ['T20', 'W02', 'W01']:
        amount_column = 'ABONOS'
    else:
        amount_column = 'ABONOS' if 'RECIBIDO' in nombre_completo.upper() else 'CARGOS'
    
    referencia = extract_reference(full_text)
    cuenta = extract_account_number(full_text)
    tipo, metodo, clasificacion = classify_transaction(code, nombre_completo, amount_column)
    nombre_resumido = create_summarized_name(nombre_completo, tipo, metodo)
    
    return {
        'fecha': fecha or "",
        'nombre': nombre_completo,
        'nombre_resumido': nombre_resumido,
        'tipo': tipo,
        'clasificacion': clasificacion,
        'quien_pago': quien,
        'monto': abs(monto),
        'referencia': referencia,
        'cuenta': cuenta,
        'metodo_pago': metodo,
        'sucursal': sucursal_default,
        'giro': ""
    }

def extract_transaction_amount_improved(lines, nombre_completo):
    """
    ULTRA MEJORADO: Detecta el monto REAL evitando saldos y números incorrectos.
    
    Reglas:
    - Números > $500,000 = saldos (ignorar)
    - Si hay número < 100 seguido de monto > 100 → tomar el segundo
    - Evitar IDs y CLABEs
    """
    for line in lines:
        line_clean = line.strip()
        
        # SKIP líneas problemáticas
        if re.search(r'\d{18}', line_clean):  # CLABE
            continue
        if re.search(r'[#\-][A-Z]*(\d{7,})', line_clean):  # IDs
            continue
        
        # Encontrar TODOS los montos con formato X,XXX.XX o XXX.XX
        amounts = re.findall(r'([\d,]+\.\d{2})', line_clean)
        
        if amounts:
            # Convertir todos a float
            parsed_amounts = []
            for amt_str in amounts:
                amt = extract_amount(amt_str)
                if amt and 0.01 <= amt <= 1000000:  # Rango válido
                    parsed_amounts.append(amt)
            
            if not parsed_amounts:
                continue
            
            # ESTRATEGIA INTELIGENTE:
            # 1. Filtrar saldos (> $500,000)
            non_balance_amounts = [a for a in parsed_amounts if a < 500000]
            
            if not non_balance_amounts:
                # Si todos son > 500k, tomar el más pequeño
                return min(parsed_amounts)
            
            # 2. Si hay número < 100, tomar el SIGUIENTE monto
            for i, amt in enumerate(non_balance_amounts):
                if amt < 100 and i + 1 < len(non_balance_amounts):
                    # Hay un número pequeño seguido de otro monto
                    next_amt = non_balance_amounts[i + 1]
                    if next_amt >= 100:  # El siguiente es razonable
                        return next_amt
            
            # 3. Tomar el monto más grande < $500k
            return max(non_balance_amounts)
        
        # Fallback: números sin decimales (comisiones pequeñas)
        simple_match = re.search(r'(\d{1,5})\s*$', line_clean)
        if simple_match:
            amount = extract_amount(simple_match.group(1))
            if amount and 0.01 <= amount <= 10000:
                return amount
    
    return None

def extract_transaction_amount_final(lines, nombre_completo):
    """
    VERSIÓN FINAL: Extrae monto EVITANDO IDs.
    
    Reglas ESTRICTAS:
    1. NO capturar números después de "#" o "-" (IDs)
    2. Solo formato moneda: XXX.XX o X,XXX.XX
    3. Al final de línea
    4. Rango: $0.01 - $1,000,000
    """
    for line in lines:
        line_clean = line.strip()
        
        # SKIP si contiene CLABE (18 dígitos)
        if re.search(r'\d{18}', line_clean):
            continue
        
        # SKIP si tiene "#" o "-" seguido de números largos (IDs)
        if re.search(r'[#\-][A-Z]*(\d{7,})', line_clean):
            continue
        
        # SKIP si la línea es principalmente código alfanumérico
        if re.search(r'[A-Z0-9]{15,}', line_clean.upper()):
            continue
        
        # Buscar monto AL FINAL de línea con formato correcto
        # Debe tener punto decimal o ser menor a 1000
        amount_match = re.search(r'([\d,]+\.\d{2})\s*$', line_clean)
        
        if amount_match:
            amount_str = amount_match.group(1)
            amount = extract_amount(amount_str)
            
            # Validar rango razonable
            if amount and 0.01 <= amount <= 1000000:
                return amount
        
        # Fallback: números pequeños sin decimales (comisiones)
        simple_match = re.search(r'(\d{1,5})\s*$', line_clean)
        if simple_match and not amount_match:
            amount_str = simple_match.group(1)
            amount = extract_amount(amount_str)
            if amount and 0.01 <= amount <= 10000:
                return amount
    
    return None


def parse_period(period_str):
    """Convierte período a datetime."""
    if not period_str:
        return None, None
    
    match = re.search(r'DEL\s+(\d{2})/(\d{2})/(\d{4})\s+AL\s+(\d{2})/(\d{2})/(\d{4})', period_str)
    if not match:
        return None, None
    
    try:
        start = datetime(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        end = datetime(int(match.group(6)), int(match.group(5)), int(match.group(4)))
        return start, end
    except:
        return None, None


def validate_balance(transactions, metadata):
    """Valida balance."""
    print("\n=== Validando Balance ===")
    
    total_ingresos = sum(t['monto'] for t in transactions if t['clasificacion'] == 'Ingreso')
    total_egresos = sum(t['monto'] for t in transactions if t['clasificacion'] == 'Egreso')
    
    declarado_depositos = float(metadata.get('total_depositos', 0))
    declarado_retiros = float(metadata.get('total_retiros', 0))
    
    diff_depositos = abs(total_ingresos - declarado_depositos)
    diff_retiros = abs(total_egresos - declarado_retiros)
    
    print(f"Depósitos - Declarado: ${declarado_depositos:,.2f} | Calculado: ${total_ingresos:,.2f} | Diff: ${diff_depositos:,.2f}")
    print(f"Retiros   - Declarado: ${declarado_retiros:,.2f} | Calculado: ${total_egresos:,.2f} | Diff: ${diff_retiros:,.2f}")
    
    tolerance = 100.00
    
    if diff_depositos > tolerance:
        print(f"⚠️ ERROR: Diferencia en depósitos superior a tolerancia")
    else:
        print("✓ Depósitos validados correctamente")
    
    if diff_retiros > tolerance:
        print(f"⚠️ ERROR: Diferencia en retiros superior a tolerancia")
    else:
        print("✓ Retiros validados correctamente")