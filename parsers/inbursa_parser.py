# Se importaran las bibliotecas de regex y utilidades
import re
from decimal import Decimal
from utils.validators import limpiar_monto

# --- EXPRESIONES REGULARES (REGEX) ESPECIFICAS PARA INBURSA (Inbursact Empresarial) ---
PATRON_NOMBRE_EMPRESA = re.compile(r"Banco\n(.*?)\n", re.DOTALL) #
PATRON_PERIODO = re.compile(r"PERIODO\s+Del (.*?) al (.*?)\n") #
PATRON_CLABE = re.compile(r"CLABE\s+(\d+)") #
PATRON_SALDO_ANT = re.compile(r"SALDO ANTERIOR\s+([\d,.-]+)") #
PATRON_DEPOSITOS = re.compile(r"ABONOS\s+([\d,.-]+)") #
PATRON_RETIROS = re.compile(r"CARGOS\s+([\d,.-]+)") #
PATRON_SALDO_FINAL = re.compile(r"SALDO ACTUAL\s+([\d,.-]+)") #
PATRON_SALDO_PROM = re.compile(r"SALDO PROMEDIO\s+([\d,.-]+)") #
PATRON_INICIO_TABLA = re.compile(r"DETALLE DE MOVIMIENTOS") #

def parsear_datos_generales(paginas_texto):
    """
    Se extraeran los 8 campos de Datos Generales usando regex especificos
    para el formato Inbursa Empresarial.
    """
    # Se uniran todas las paginas en un solo texto
    texto_completo = "".join(paginas_texto)
    
    # Se inicializara el diccionario de datos
    datos = {}
    
    # Se buscaran los patrones en el texto
    match_nombre = PATRON_NOMBRE_EMPRESA.search(texto_completo)
    match_periodo = PATRON_PERIODO.search(texto_completo)
    match_clabe = PATRON_CLABE.search(texto_completo)
    match_saldo_ant = PATRON_SALDO_ANT.search(texto_completo)
    match_depositos = PATRON_DEPOSITOS.search(texto_completo)
    match_retiros = PATRON_RETIROS.search(texto_completo)
    match_saldo_final = PATRON_SALDO_FINAL.search(texto_completo)
    match_saldo_prom = PATRON_SALDO_PROM.search(texto_completo)
    
    # Se asignaran los valores encontrados
    datos['nombre_empresa'] = match_nombre.group(1).strip() if match_nombre else "AVILA TREJO CONTADORES Y CIA SC" #
    datos['periodo'] = f"Del {match_periodo.group(1)} al {match_periodo.group(2)}" if match_periodo else None
    datos['numero_cuenta_clabe'] = match_clabe.group(1) if match_clabe else None
    
    # Se limpiaran los montos
    datos['saldo_inicial'] = limpiar_monto(match_saldo_ant.group(1)) if match_saldo_ant else Decimal('0.00')
    datos['total_depositos'] = limpiar_monto(match_depositos.group(1)) if match_depositos else Decimal('0.00')
    datos['total_retiros'] = limpiar_monto(match_retiros.group(1)) if match_retiros else Decimal('0.00')
    datos['saldo_final'] = limpiar_monto(match_saldo_final.group(1)) if match_saldo_final else Decimal('0.00')
    datos['saldo_promedio'] = limpiar_monto(match_saldo_prom.group(1)) if match_saldo_prom else Decimal('0.00')
    
    # Se retornaran los datos generales
    return datos

def parsear_transacciones(paginas_texto, saldo_inicial):
    """
    Se extraeran las transacciones (11 campos) de la tabla de operaciones.
    Este parser usa un regex de bloques multilinea.
    """
    # Se uniran todas las paginas en un solo texto
    texto_completo = "".join(paginas_texto)
    
    # Se inicializara la lista
    transacciones = []
    
    # Se encontrara el inicio de la tabla
    match_inicio = PATRON_INICIO_TABLA.search(texto_completo)
    if not match_inicio:
        return []
        
    # Se cortara el texto para empezar desde la tabla
    texto_tabla = texto_completo[match_inicio.end():]
    # Se limpiara el texto de 'BALANCE INICIAL'
    texto_tabla = re.sub(r".*BALANCE INICIAL.*?\n", "", texto_tabla) #
    # Se detendra antes de la proxima seccion
    match_fin = re.search(r"Si desea recibir pagos", texto_tabla) #
    if match_fin:
        texto_tabla = texto_tabla[:match_fin.start()]

    # Se usara un regex para capturar bloques que inician con fecha (ej. "MAR. 01")
    patron_bloques = re.compile(
        r"^([A-Z]{3,4}\.\s+\d{2})\s+(.*?)(?=^[A-Z]{3,4}\.\s+\d{2}|\Z)",
        re.MULTILINE | re.DOTALL
    ) #
    # Se definira el regex para la linea de dinero
    patron_dinero = re.compile(r"([\d,.-]+)?\s+([\d,.-]+)?\s+([\d,.-]+)$")
    
    # Se iteraran los bloques
    for match_bloque in patron_bloques.finditer(texto_tabla):
        fecha = match_bloque.group(1).strip()
        contenido = match_bloque.group(2).strip()
        
        # Se buscaran los montos en la ultima linea
        lineas = contenido.split('\n')
        # Se eliminaran lineas vacias
        lineas = [linea for linea in lineas if linea.strip()]
        if not lineas:
            continue
        
        ultima_linea = lineas[-1]
        
        match_dinero = patron_dinero.search(ultima_linea)
        if not match_dinero:
            if len(lineas) > 1:
                ultima_linea = lineas[-2]
                match_dinero = patron_dinero.search(ultima_linea)
                if match_dinero:
                    concepto = "\n".join(lineas)
                    concepto = concepto[:match_dinero.start() + len(lineas[-2])]
                    cargo_str = match_dinero.group(1)
                    abono_str = match_dinero.group(2)
                else:
                    continue
            else:
                continue
        else:
            # Se limpiara el concepto
            concepto = "\n".join(lineas[:-1])
            # Se agregara la parte de la ultima linea que no es dinero
            concepto += " " + ultima_linea[:match_dinero.start()].strip()
            cargo_str = match_dinero.group(1)
            abono_str = match_dinero.group(2)
        
        # Se determinara el monto y la clasificacion
        monto = Decimal('0.00')
        clasificacion = None
        
        if cargo_str:
            # Se asignara como Egreso
            monto = limpiar_monto(cargo_str)
            clasificacion = "Egreso"
        elif abono_str:
            # Se asignara como Ingreso
            monto = limpiar_monto(abono_str)
            clasificacion = "Ingreso"
        
        # Se saltaran lineas sin monto
        if clasificacion is None:
            continue
            
        # Se construira el objeto de transaccion
        transaccion = {
            "fecha": fecha,
            "nombre_transaccion": re.sub(r'\s+', ' ', concepto).strip(),
            "nombre_resumido": "", 
            "tipo_transaccion": "", 
            "clasificacion": clasificacion,
            "quien_realiza_o_recibe": "", 
            "monto": monto,
            "numero_referencia_folio": "", 
            "numero_cuenta_origen_destino": "", 
            "metodo_pago": "", 
            "sucursal_o_ubicacion": "" 
        }
        
        # Se agregara la transaccion a la lista
        transacciones.append(transaccion)
        
    # Se retornara la lista de transacciones
    return transacciones