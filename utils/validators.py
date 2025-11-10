# -*- coding: utf-8 -*-
"""
Validadores - VERSIÓN ULTRA OPTIMIZADA
Validación estricta sin falsos positivos
"""

import re
from decimal import Decimal, InvalidOperation
from thefuzz import fuzz

def limpiar_monto(texto_monto):
    """
    Convierte un string de monto (ej. "$1,234.55") a un objeto Decimal.
    """
    # Verificar si el input es nulo o vacío
    if texto_monto is None:
        return Decimal('0.00')
    # Convertir a string para manejar números
    texto_str = str(texto_monto)
    # Eliminar caracteres no numéricos (excepto el punto decimal y el signo negativo)
    texto_limpio = re.sub(r"[^\d.-]", "", texto_str)
    if not texto_limpio:
        # Retornar Cero si el string está vacío
        return Decimal('0.00')
    try:
        # Convertir a Decimal
        return Decimal(texto_limpio)
    except InvalidOperation:
        # Manejar un error de conversión
        return Decimal('0.00')

def validar_balance(datos_generales, transacciones):
    """
    Valida que los cálculos matemáticos del balance sean correctos.
    Verifica que: Saldo Final = Saldo Inicial + Depósitos - Retiros.
    VERSIÓN ESTRICTA: No reporta OK si hay errores.
    """
    # Inicializar el reporte de validación
    reporte = {"balance_coherente": False, "totales_coherentes": False, "mensajes": []}
    
    try:
        # Limpiar los montos de los datos generales
        saldo_inicial = limpiar_monto(datos_generales.get('saldo_inicial', '0'))
        saldo_final = limpiar_monto(datos_generales.get('saldo_final', '0'))
        total_depositos_declarado = limpiar_monto(datos_generales.get('total_depositos', '0'))
        total_retiros_declarado = limpiar_monto(datos_generales.get('total_retiros', '0'))
        
        # Calcular los totales de las transacciones extraídas
        total_depositos_calculado = Decimal('0.00')
        total_retiros_calculado = Decimal('0.00')
        
        # Iterar sobre cada transacción
        for tx in transacciones:
            # Obtener el monto
            monto = limpiar_monto(tx.get('monto', '0'))
            # Sumar al total correspondiente
            if tx.get('clasificacion') == 'Ingreso':
                total_depositos_calculado += monto
            elif tx.get('clasificacion') == 'Egreso':
                total_retiros_calculado += monto

        # Definir una tolerancia para comparaciones decimales (ej. 1 centavo)
        tolerancia = Decimal('0.01')
        
        # --- Validación 1: Coherencia de Totales ---
        totales_ok = True
        
        diferencia_depositos = abs(total_depositos_declarado - total_depositos_calculado)
        diferencia_retiros = abs(total_retiros_declarado - total_retiros_calculado)
        
        if diferencia_depositos <= tolerancia and diferencia_retiros <= tolerancia:
            # Marcar como coherente
            reporte["totales_coherentes"] = True
            reporte["mensajes"].append("✓ Validacion de Totales: OK.")
        else:
            # Marcar como incoherente
            totales_ok = False
            reporte["totales_coherentes"] = False
            
            if diferencia_depositos > tolerancia:
                reporte["mensajes"].append(
                    f"✗ ERROR Totales Depositos: Declarado ${total_depositos_declarado}, "
                    f"Calculado ${total_depositos_calculado}, Diferencia ${diferencia_depositos}"
                )
            
            if diferencia_retiros > tolerancia:
                reporte["mensajes"].append(
                    f"✗ ERROR Totales Retiros: Declarado ${total_retiros_declarado}, "
                    f"Calculado ${total_retiros_calculado}, Diferencia ${diferencia_retiros}"
                )

        # --- Validación 2: Coherencia de Balance ---
        saldo_final_calculado = saldo_inicial + total_depositos_declarado - total_retiros_declarado
        diferencia_balance = abs(saldo_final - saldo_final_calculado)
        
        if diferencia_balance <= tolerancia:
            # Marcar como coherente
            reporte["balance_coherente"] = True
            
            # CRÍTICO: Solo reportar OK si AMBAS validaciones pasaron
            if totales_ok:
                reporte["mensajes"].append("✓ Validacion de Balance: OK.")
            else:
                reporte["mensajes"].append(
                    "⚠ Validacion de Balance: Formula correcta, pero totales no coinciden."
                )
        else:
            # Marcar como incoherente
            reporte["balance_coherente"] = False
            reporte["mensajes"].append(
                f"✗ ERROR Balance: Saldo Final Declarado ${saldo_final}, "
                f"Calculado ${saldo_final_calculado}, Diferencia ${diferencia_balance}"
            )

    except Exception as e:
        # Manejar cualquier error inesperado
        reporte["mensajes"].append(f"✗ Excepcion en validacion: {e}")
        
    # Retornar el reporte
    return reporte

def validar_cruzada(resultado_a, resultado_b):
    """
    Realiza comparaciones de alto nivel entre dos extracciones (Nativo vs OCR)
    para generar un puntaje de confianza.
    """
    # Inicializar el reporte
    reporte = {"puntaje_confianza": 0.0, "mensajes": []}
    
    try:
        # Comparar los datos generales usando similitud de strings
        json_a = str(resultado_a.get('datos_generales', {}))
        json_b = str(resultado_b.get('datos_generales', {}))
        similitud_generales = fuzz.ratio(json_a, json_b)
        reporte["mensajes"].append(f"Similitud Datos Generales: {similitud_generales}%")
        
        # Comparar la cantidad de transacciones
        len_a = len(resultado_a.get('transacciones', []))
        len_b = len(resultado_b.get('transacciones', []))
        reporte["mensajes"].append(f"Conteo Transacciones (Nativo: {len_a}, OCR: {len_b})")
        
        # Calcular un puntaje simple de confianza
        puntaje = (similitud_generales / 100.0)
        if max(len_a, len_b) > 0:
            # Penalizar la diferencia en conteo
            puntaje_conteo = min(len_a, len_b) / max(len_a, len_b)
            puntaje = (puntaje + puntaje_conteo) / 2.0
            
        # Asignar el puntaje final
        reporte["puntaje_confianza"] = round(puntaje, 2)
        reporte["mensajes"].append(f"Puntaje de Confianza Final: {reporte['puntaje_confianza']}")
        
    except Exception as e:
        reporte["mensajes"].append(f"Error en validación cruzada: {e}")
        reporte["puntaje_confianza"] = 0.0
    
    # Retornar el reporte
    return reporte