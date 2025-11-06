# Se importaran las bibliotecas necesarias
import re
from decimal import Decimal, InvalidOperation
from thefuzz import fuzz

def limpiar_monto(texto_monto):
    """
    Se convertira un string de monto (ej. "$1,234.55") a un objeto Decimal.
    """
    # Se verificara si el input es nulo o vacio
    if texto_monto is None:
        return Decimal('0.00')
    # Se convertira a string para manejar numeros
    texto_str = str(texto_monto)
    # Se eliminaran caracteres no numericos (excepto el punto decimal y el signo negativo)
    texto_limpio = re.sub(r"[^\d.-]", "", texto_str)
    if not texto_limpio:
        # Se retornara Cero si el string esta vacio
        return Decimal('0.00')
    try:
        # Se convertira a Decimal
        return Decimal(texto_limpio)
    except InvalidOperation:
        # Se manejara un error de conversion
        return Decimal('0.00')

def validar_balance(datos_generales, transacciones):
    """
    Se validara que los calculos matematicos del balance sean correctos.
    Se verificara que: Saldo Final = Saldo Inicial + Depositos - Retiros.
    """
    # Se inicializara el reporte de validacion
    reporte = {"balance_coherente": False, "totales_coherentes": False, "mensajes": []}
    
    try:
        # Se limpiaran los montos de los datos generales
        saldo_inicial = limpiar_monto(datos_generales.get('saldo_inicial', '0'))
        saldo_final = limpiar_monto(datos_generales.get('saldo_final', '0'))
        total_depositos_declarado = limpiar_monto(datos_generales.get('total_depositos', '0'))
        total_retiros_declarado = limpiar_monto(datos_generales.get('total_retiros', '0'))
        
        # Se calcularan los totales de las transacciones extraidas
        total_depositos_calculado = Decimal('0.00')
        total_retiros_calculado = Decimal('0.00')
        
        # Se iterara sobre cada transaccion
        for tx in transacciones:
            # Se obtendra el monto
            monto = limpiar_monto(tx.get('monto', '0'))
            # Se sumara al total correspondiente
            if tx.get('clasificacion') == 'Ingreso':
                total_depositos_calculado += monto
            elif tx.get('clasificacion') == 'Egreso':
                total_retiros_calculado += monto

        # Se definira una tolerancia para comparaciones decimales (ej. 1 centavo)
        tolerancia = Decimal('0.01')
        
        # --- Validacion 1: Coherencia de Totales ---
        if abs(total_depositos_declarado - total_depositos_calculado) <= tolerancia and \
           abs(total_retiros_declarado - total_retiros_calculado) <= tolerancia:
            # Se marcara como coherente
            reporte["totales_coherentes"] = True
            reporte["mensajes"].append("Validacion de Totales: OK.")
        else:
            # Se registrara la discrepancia
            reporte["mensajes"].append(f"ERROR Totales: Depositos (Declarado: {total_depositos_declarado}, Calculado: {total_depositos_calculado})")
            reporte["mensajes"].append(f"ERROR Totales: Retiros (Declarado: {total_retiros_declarado}, Calculado: {total_retiros_calculado})")

        # --- Validacion 2: Coherencia de Balance ---
        saldo_final_calculado = saldo_inicial + total_depositos_declarado - total_retiros_declarado
        
        if abs(saldo_final - saldo_final_calculado) <= tolerancia:
            # Se marcara como coherente
            reporte["balance_coherente"] = True
            reporte["mensajes"].append("Validacion de Balance: OK.")
        else:
            # Se registrara la discrepancia
            reporte["mensajes"].append(f"ERROR Balance: Saldo Final (Declarado: {saldo_final}, Calculado: {saldo_final_calculado})")

    except Exception as e:
        # Se manejara cualquier error inesperado
        reporte["mensajes"].append(f"Excepcion en validacion: {e}")
        
    # Se retornara el reporte
    return reporte

def validar_cruzada(resultado_a, resultado_b):
    """
    Se realizaran comparaciones de alto nivel entre dos extracciones (Nativo vs OCR)
    para generar un puntaje de confianza.
    """
    # Se inicializara el reporte
    reporte = {"puntaje_confianza": 0.0, "mensajes": []}
    
    # Se compararan los datos generales usando similitud de strings
    json_a = str(resultado_a.get('datos_generales', {}))
    json_b = str(resultado_b.get('datos_generales', {}))
    similitud_generales = fuzz.ratio(json_a, json_b)
    reporte["mensajes"].append(f"Similitud Datos Generales: {similitud_generales}%")
    
    # Se comparara la cantidad de transacciones
    len_a = len(resultado_a.get('transacciones', []))
    len_b = len(resultado_b.get('transacciones', []))
    reporte["mensajes"].append(f"Conteo Transacciones (Nativo: {len_a}, OCR: {len_b})")
    
    # Se calculara un puntaje simple de confianza
    puntaje = (similitud_generales / 100.0)
    if max(len_a, len_b) > 0:
        # Se penalizara la diferencia en conteo
        puntaje_conteo = min(len_a, len_b) / max(len_a, len_b)
        puntaje = (puntaje + puntaje_conteo) / 2.0
        
    # Se asignara el puntaje final
    reporte["puntaje_confianza"] = round(puntaje, 2)
    reporte["mensajes"].append(f"Puntaje de Confianza Final: {reporte['puntaje_confianza']}")
    
    # Se retornara el reporte
    return reporte