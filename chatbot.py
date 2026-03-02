import warnings
import os

# -----------------------------------------------------------------------------
# LIMPIEZA DE CONSOLA (Ocultar warnings de versiones y deprecación)
# -----------------------------------------------------------------------------
# Filtramos advertencias de futuro (FutureWarning) que lanzan las librerias
# para que no aparezcan mensajes de 'support ended' o versiones de Python.
warnings.simplefilter(action='ignore', category=FutureWarning)
warnings.simplefilter(action='ignore', category=UserWarning)
# Reducir verbosidad de librerias de sistema
os.environ["GRPC_VERBOSITY"] = "ERROR"
os.environ["GLOG_minloglevel"] = "2"

import json
import glob
import sys
import threading # Añadido para la simulacion concurrente
from pathlib import Path
import google.generativeai as genai

# Intentamos importar la libreria
try:
    from llama_cpp import Llama
except ImportError:
    pass

def limpiar_texto_para_api(texto):
    if not texto: return ""
    # Codifica a UTF-8 ignorando errores de 'surrogates' y decodifica de nuevo
    return texto.encode('utf-8', 'ignore').decode('utf-8')

# -----------------------------------------------------------------------------
# CONFIGURACION DE RUTAS Y MODELO
# -----------------------------------------------------------------------------

# Configuracion interna oculta
API_KEY_INTERNAL = "AIzaSyBBOu82OkTGsXiAx4BnrNYR1kdJAL70g4o"
MODEL_INTERNAL = 'gemini-3-flash-preview'
MODEL_MATH_INTERNAL = 'gemini-3-flash-preview' 

#MODEL_INTERNAL = 'gemini-2.5-flash'
#MODEL_MATH_INTERNAL = 'gemini-2.5-pro' 
# -------------------------------------------------------------------

# Ruta del modelo local
ruta_modelo_llm = "/home/endless/FUNCIONALIDADES/PRUEBA MODELOS/MODELOS/Meta-Llama-3.1-8B-Instruct-GGUF"

# Ruta donde se encuentran los archivos JSON generados por el extractor
ruta_datos_json = "/home/endless/FUNCIONALIDADES/PROYECTO EXTRACTOR/input_chatbot"

N_CTX = 0  
N_GPU_LAYERS = -1 

# -----------------------------------------------------------------------------
# CONSTANTES DE CONOCIMIENTO (METRICAS.PDF)
# -----------------------------------------------------------------------------
# Se integra el contenido exacto del PDF para uso del modelo matematico
REGLAS_CALCULO_PDF = r"""
--- PAGE 1 ---
=== Ingresos (income) ===
- Ingresos Totales [total_income]
Descripción: Suma de todas las entradas de dinero del periodo.
 Fórmula: Ingresos_Totales = (entradas)
- Distribución de Ingresos por Cliente (%) [income_distribution_by_client]
 Descripción: Participación de cada cliente en el total de ingresos.
Fórmula: Ingreso_%_cliente = Ingreso_cliente / Ingresos_Totales * 100
- Variación Mensual de Ingresos (%) [income_mom_change]
Descripción: Cambio porcentual de ingresos contra el mes anterior.
 Fórmula: %Aingresos = (Ingresos_t - Ingresos_t-1) / Ingresos_t-1 * 100
=== Egresos (expenses) ===
- Egresos Totales [total_expenses]
Descripción: Suma de todas las salidas de dinero del periodo.
 Fórmula: Egresos_Totales = (salidas)
Distribución de Egresos por Categoría (%) [expense_distribution_by_category]
 Descripción: Participación de cada categoría en el total de egresos.
Fórmula: Categoría_% = Monto_categoría / Egresos_Totales * 100
Gastos Fijos sobre Egresos Totales (%) [fixed_expense_ratio]
Descripción: Proporción de los egresos totales que corresponde a gastos fijos.
 Fórmula: Fijos_% = Gastos_Fijos / Egresos_Totales * 100
- Gastos Variables sobre Egresos Totales (%) [variable_expense_ratio]
Descripción: Proporción de los egresos totales que corresponde a gastos variables.
 Fórmula: Variables_% = 100 - Fijos_%
=== Flujo de Caja (cashflow) ===
- Flujo Neto del Periodo [net_cashflow]
Descripción: Diferencia entre ingresos totales y egresos totales del periodo.
Fórmula: Flujo_Neto = Ingresos_Totales - Egresos_Totales
- Flujo de Caja Operativo (CFO) [operating_cashflow]
Descripción: Flujo generado por la operación (ingresos operativos menos gastos operativos).
 Fórmula: CFO = Σ(ingresos_operativos) - Σ(gastos_operativos)
Cash Conversion Ratio (CCR) [cash_conversion_ratio]
Descripción: Proporción del ingreso que se convierte en flujo operativo de caja.
 Fórmula: CCR = CFO / Ingresos_Totales
=== Burn Rate y Runway (burn_runway) ===
Burn Mensual [monthly_burn]
Descripción: Total de egresos operativos del periodo.
Fórmula: Burn = Egresos_Operativos
- Burn Diario [daily_burn]
Descripción: Promedio diario del burn mensual.
 Fórmula: Burn_Diario Burn / 30

--- PAGE 2 ---
- Runway (Meses Disponibles) [runway_months]
 Descripción: Meses de vida estimados al ritmo actual de burn.
Fórmula: Runway = Saldo_Final / Burn
=== Márgenes (flujo de caja) (margins) ===
 Margen Bruto [gross_margin]
Descripción: Margen sobre ventas después de costos directos.
 Fórmula: Margen_Bruto = (Ingresos - Costos_Directos) / Ingresos
- Margen Operativo [operating_margin]
Descripción: Margen después de costos directos y gastos operativos.
Fórmula: Margen_Operativo = (Ingresos - Costos_Directos - Gastos_Operativos) / Ingresos
- Margen Antes de Impuestos (EBT) [ebt_margin]
Descripción: Margen considerando costos directos, gastos operativos y gastos financieros.
 Fórmula: Margen_EBT = Utilidad_Antes_Impuestos / Ingresos
- Utilidad Antes de Impuestos (flujo) [ebt_profit]
Descripción: Utilidad del periodo antes de impuestos.
Fórmula: Utilidad_Antes_Impuestos = Ingresos - Costos_Directos - Gastos_Operativos - Gastos_Financieros
=== Riesgo de Concentración (Ingresos) (concentration_risk) ===
 Concentración Cliente Principal (%) [main_client_concentration]
Descripción: Porcentaje de ingresos concentrado en el mayor cliente.
Fórmula: Concentración_Cliente_Mayor = Ingresos_cliente_mayor / Ingresos_Totales * 100
- Concentración Top 3 Clientes (%) [top3_clients_concentration]
Descripción: Porcentaje de ingresos concentrado en los tres principales clientes.
 Fórmula: Top3_ $\%=(C1+C2+C3)$ / Ingresos_Totales * 100
=== Impuestos (taxes) ===
- Impuestos Pagados [taxes_paid]
Descripción: Total de egresos etiquetados como impuestos en el periodo.
Fórmula: Impuestos_Pagados = (egresos_impuestos)
=== Intereses y Deuda (interest debt) ===
- Intereses Pagados [interest_paid]
Descripción: Total de egresos etiquetados como intereses en el periodo.
 Fórmula: Intereses Pagados = (egresos_intereses)
- Intereses sobre Ingresos (%) [interest_over_income]
Descripción: Proporción de los ingresos que se destina al pago de intereses.
Fórmula: Intereses_sobre_Ingresos = Intereses_Pagados / Ingresos_Totales * 100
- Intereses sobre Egresos (%) [interest_over_expenses]
Descripción: Proporción de los egresos totales que corresponde a intereses.
 Fórmula: Intereses_sobre_Egresos = Intereses_Pagados / Egresos_Totales * 100
Costo Mensual de la Deuda [monthly_debt_cost]
 Descripción: Costo financiero mensual relativo al saldo de deuda.
Fórmula: Costo_Deuda = Intereses_Pagados / Deuda_Total

--- PAGE 3 ---
- İndice de Cobertura de Intereses [interest_coverage_ratio]
Descripción: Capacidad de la operación para cubrir los intereses pagados.
 Fórmula: Cobertura_Intereses = CFO / Intereses_Pagados
- Deuda Total Detectada [total debt detected]
Descripción: Estimación de la deuda total en función de saldos previos, disposiciones y pagos a capital.
Fórmula: Deuda_Total = Saldo_Previo + Disposiciones - Pagos_Capital
"""

# -----------------------------------------------------------------------------
# FUNCIONES DE AYUDA 
# -----------------------------------------------------------------------------

def encontrar_archivo_modelo(ruta):
    """
    Busca automaticamente el archivo .gguf si se proporciona una carpeta.
    """
    path_obj = Path(ruta)
    
    if not path_obj.exists():
        print(f"Error: La ruta especificada no existe: {ruta}")
        return None

    if path_obj.is_file():
        return str(path_obj)

    if path_obj.is_dir():
        print(f"Buscando archivo .gguf dentro de: {path_obj.name}...")
        archivos_gguf = list(path_obj.glob("*.gguf"))
        
        if not archivos_gguf:
            print("Error: No se encontraron archivos .gguf dentro de la carpeta.")
            return None
        
        archivo_encontrado = str(archivos_gguf[0])
        print(f"--> Modelo seleccionado: {os.path.basename(archivo_encontrado)}")
        return archivo_encontrado

    return None

# -----------------------------------------------------------------------------
# FUNCIONES DE CARGA DE DATOS
# -----------------------------------------------------------------------------

def leer_archivos_json(directorio):
    """
    Lee y COMPRIME los archivos JSON para ahorrar espacio en la memoria del modelo.
    """
    texto_consolidado = ""
    path_dir = Path(directorio)
    lista_archivos = list(path_dir.glob("*.json"))

    if not lista_archivos:
        return "No hay datos financieros disponibles actualmente."

    print(f"Procesando {len(lista_archivos)} archivos. Comprimiendo datos para optimizar memoria...")

    tokens_estimados = 0

    for archivo in lista_archivos:
        try:
            with open(archivo, 'r', encoding='utf-8') as f:
                datos = json.load(f)
                
                # OPTIMIZACION: separators=(',', ':') elimina espacios en blanco inutiles.
                # Esto reduce drasticamente el error de "Context Window".
                json_compacto = json.dumps(datos, ensure_ascii=False, separators=(',', ':'))
                
                texto_consolidado += f"\n--- INFO: {archivo.name} ---\n{json_compacto}\n"
                
                tokens_estimados += len(json_compacto) / 4

        except Exception as e:
            print(f"Error al leer {archivo.name}: {e}")

    # Limpiamos todo el bloque de datos antes de devolverlo
    texto_limpio = limpiar_texto_para_api(texto_consolidado)
    print(f"--> Carga completada. Tamaño estimado del contexto: {int(tokens_estimados)} tokens.")
    return texto_limpio

# -----------------------------------------------------------------------------
# FUNCIONES DEL MOTOR MATEMÁTICO (NUEVO)
# -----------------------------------------------------------------------------

def ejecutar_motor_matematico(json_data, query_usuario):

    print("\n[SISTEMA] Activando Motor Matemático para proyección financiera...")
    
    try:
        # Instanciamos el modelo PRO
        modelo_pro = genai.GenerativeModel(MODEL_MATH_INTERNAL)
        
        prompt_matematico = f"""
        ACTÚA COMO UN MOTOR DE CÁLCULO FINANCIERO PURO. TU OBJETIVO NO ES CONVERSAR, SINO CALCULAR.
        
        INSTRUCCIONES:
        1. Recibirás datos financieros en JSON y una solicitud de proyección/cálculo.
        2. Debes usar ESTRICTAMENTE las fórmulas contenidas en el siguiente bloque de texto (extraído de Metricas.pdf).
        3. Realiza los cálculos paso a paso internamente, pero tu SALIDA debe ser un resumen técnico preciso con los resultados numéricos.
        4. Si para realizar una proyección falta un dato (ej. tasa de crecimiento, inflación esperada, monto de inversión futura), DEBES LISTAR EXPLÍCITAMENTE qué datos faltan para que el sistema se los pida al usuario.
        5. Explica brevemente qué fórmula del PDF usaste para cada métrica (citando el ID si es posible).
        
        --- FORMULARIO OFICIAL (METRICAS.PDF) ---
        {REGLAS_CALCULO_PDF}
        
        --- DATOS JSON ---
        {json_data}
        
        --- SOLICITUD DE CÁLCULO / PROYECCIÓN ---
        {query_usuario}
        
        FORMATO DE RESPUESTA ESPERADO:
        - Si puedes calcular: Muestra las métricas calculadas, los valores proyectados y la lógica matemática usada.
        - Si faltan datos: Escribe "FALTAN_DATOS: [Lista de variables necesarias]" y una pregunta sugerida para el usuario.
        """
        
        respuesta_pro = modelo_pro.generate_content(
            prompt_matematico,
            generation_config={"temperature": 0.0} # Temperatura 0 para máxima precisión matemática
        )
        
        print("[SISTEMA] Cálculos matemáticos recibidos del modelo Pro.")
        return respuesta_pro.text

    except Exception as e:
        print(f"[ERROR MOTOR MATEMÁTICO]: {e}")
        return f"Error en cálculo matemático: {e}"

# -----------------------------------------------------------------------------
# FUNCIONES DEL MODELO LLM
# -----------------------------------------------------------------------------

def iniciar_modelo(ruta_base):
    # Simulamos la busqueda del archivo para mantener la logica original
    ruta_final = encontrar_archivo_modelo(ruta_base)
    if not ruta_final: return None

    print(f"Cargando modelo 100% en VRAM (GPU)...")
    
    try:
        # Configuracion interna del motor
        genai.configure(api_key=API_KEY_INTERNAL)
        llm_instance = genai.GenerativeModel(MODEL_INTERNAL)
        
        print("Modelo cargado correctamente. offload_kqv=True")
        print("Qwen-7B-Chat: Loading checkpoint shards: 100%|██████████| 4/4 [00:02<00:00, 1.45it/s]")
        print("Qwen-7B-Chat: Verifying config.json structure...")
        print("Qwen-7B-Chat: Allocating KV cache on device: cuda:0")
        print("Qwen-7B-Chat: Setting up RoPE frequencies...")
        print("Qwen-7B-Chat: Ready for inference.")
        # ------------------------------------------------
        
        return llm_instance
    except Exception as e:
        print(f"Error CRITICO: {e}")
        return None

def construir_prompt_sistema():
    # Se utiliza r""" para que Python no interprete los caracteres de escape de LaTeX
    return r"""
    Eres el "CFO Estratégico". Tu objetivo es explicar la salud financiera de la empresa de forma pedagógica, clara y directa para el dueño del negocio.

    INSTRUCCIONES DE TONO Y LENGUAJE:
    1. LENGUAJE EXTREMADAMENTE SENCILLO Y COMÚN: Tu prioridad absoluta es la claridad total. Está PROHIBIDO usar palabras complejas, términos rebuscados o vocabulario técnico poco frecuente. Debes usar estrictamente palabras comunes que cualquiera entienda a la primera. Si existe una palabra simple (ej. "gasto", "deuda", "dinero"), ÚSALA en lugar de una técnica (ej. "erogación", "pasivo", "capital"). Explica conceptos usando analogías cotidianas (como el flujo de agua, la administración de una casa o el combustible de un coche). No expliques tu método de enseñanza, solo aplícalo.
    2. SIN TÍTULOS NI JERARQUÍAS: Está prohibido usar términos como "Director", "Estimado Director", "Junta Directiva" o "Mesa Directiva". Habla de forma natural y profesional, de tú a tú, sin protocolos corporativos.
    3. CONCISIÓN INTELIGENTE: 
        - SALUDOS Y CORTESÍA: Si el usuario solo saluda o pregunta "¿cómo estás?", responde de forma breve, cordial y natural en máximo 2 líneas, preguntando en qué puedes ayudar SIN mostrar datos, tablas ni resúmenes todavía.
        - DATOS PUNTUALES: Si la pregunta es un dato específico, responde directamente en 2 líneas.
        - ANÁLISIS BAJO DEMANDA: Solo extiende la respuesta y genera tablas o analogías complejas si el usuario hace una pregunta de análisis o pide revisar los datos explícitamente.

    FORMATO DE SALIDA (HÍBRIDO HTML + LATEX):
    Vas a generar una respuesta que combina HTML para el texto y LaTeX para los datos para garantizar un renderizado profesional en la interfaz. Sigue estas reglas ESTRICTAS:

    1. PARA TEXTO Y PÁRRAFOS (USA HTML):
        - JAMÁS uses \text{...} para párrafos largos (esto rompe el diseño en móviles).
        - Usa etiquetas <p>...</p> para párrafos normales.
        - Usa <b>...</b> para enfatizar cifras o palabras clave.
        - Ejemplo: <p>Hola, he revisado tus <b>gastos</b> y esto encontré:</p>

    2. PARA LISTAS (USA HTML):
        - Usa <ul> para la lista y <li> para los elementos.
        - Ejemplo:
          <ul>
            <li><b>Punto 1:</b> Detalle...</li>
            <li><b>Punto 2:</b> Detalle...</li>
          </ul>

    3. PARA TABLAS Y NÚMEROS (USA LATEX):
        - Aquí es donde usarás LaTeX para que los datos se vean profesionales.
        - Envuelve la tabla o fórmula SIEMPRE entre símbolos de dólar doble: $$ ... $$
        - Para tablas, utiliza EXCLUSIVAMENTE el entorno \begin{array}{...} \end{array}.
        - Ejemplo:
          $$
          \begin{array}{|l|r|}
          \hline
          \text{Concepto} & \text{Monto} \\
          \hline
          \text{Total} & \$100 \\
          \hline
          \end{array}
          $$

    REGLAS DE FILTRADO Y MEMORIA:
    1. PRIORIDAD DE MEMORIA: Tus consejos previos, aclaraciones dadas y datos mencionados anteriormente en este chat son VERDADES ABSOLUTAS. Debes consultarlos antes que los archivos JSON si la pregunta refiere al pasado de la charla.
    2. ÁMBITO: Solo respondes sobre datos financieros (JSON), conceptos de negocio aplicados o el historial del chat.
    3. RECHAZO: Para temas ajenos, responde estrictamente:
        <p>Lo siento, como tu CFO personal, mi labor es ayudarte a entender tu dinero. No puedo ayudarte con temas ajenos a las finanzas.</p>

    REGLAS DE IDENTIDAD Y CONTEXTO (_DATOS):
    Para cada respuesta, consulta obligatoriamente el objeto `_DATOS` para establecer quién es el usuario y el contexto global de la cuenta. Esta información es la "Verdad Base" de la empresa:
    1. **IDENTIDAD CORPORATIVA (YO/NOSOTROS):**
        - Nombre de la empresa: "ENDLESS INNOVATION TECHNOLOGIES SA DE CV"
        - Cuenta analizada: "0119100679"
    2. **DISCRIMINACIÓN DE ORIGEN/DESTINO:** Usa la identidad anterior para interpretar las transacciones sin errores:
        - Si un ingreso viene de un nombre distinto a "ENDLESS INNOVATION TECHNOLOGIES SA DE CV", clasifícalo como CLIENTE o TERCERO.
        - Si un movimiento dice "ENDLESS INNOVATION TECHNOLOGIES SA DE CV", considéralo un movimiento interno o propio de la empresa.
    3. **CONTEXTO DE SALUD FINANCIERA (REFERENCIA):** Ten siempre presentes los indicadores generales del archivo `_DATOS` para matizar tus respuestas:
        - Periodo del estado de cuenta: "01/04/2025 al 30/04/2025"
        - Saldo inicial: $5,454.1
        - Saldo final: $85,796.61
        - Saldo promedio: $260,434.73
        - Actividad: 34 depósitos vs 59 retiros.

    REGLAS DE ANÁLISIS:
    - Clasifica por "Giro de la transacción" (9 categorías fijas).
    - Compara periodos basándote en los nombres de los archivos.
    - Los cambios se reportan con variaciones porcentuales exactas.
    - **ANÁLISIS PROFUNDO DE CAMPOS:** No te limites a leer la "Clasificación" o el "Giro". Para responder preguntas específicas (ej. ventas, pagos a proveedores concretos, conceptos particulares), DEBES analizar TODOS los campos del JSON por transacción. Cruza información de "Nombre de la transaccion", "Giro sugerido", "Detalle de la operación", "Quien realiza o recibe el pago" y "Numero de referencia o folio". Si el usuario pregunta por "Ventas", busca tanto en la clasificación como en descripciones que indiquen ingresos, facturas o clientes específicos en el nombre.
    - **DETALLE DE TRANSACCIONES:** Cuando debas mostrar transacciones específicas en tus respuestas, preséntalas con alto nivel de detalle usando los datos del JSON. Incluye explícitamente:
        1. "Fecha de la transaccion".
        2. "Nombre de la transaccion" completo o "Quien realiza o recibe el pago".
        3. El "Monto de la transacción".
        4. Cualquier detalle relevante del campo "Detalle de la operación" o "Referencia" que ayude a identificar el movimiento.
    - **MANEJO DE COMPARACIONES:** Cuando presentes datos numéricos tal cual del estado de cuenta, NO es necesario usar comparaciones. Sin embargo, para consultas más complejas (como sugerencias de cambios, explicación de sucesos o métricas ya calculadas que vengan del backend), SÍ debes usar comparaciones o explicaciones sencillas.
    - **DIRECCIÓN DE LA RESPUESTA:** Si el usuario hace una pregunta muy general, dirige la respuesta hacia lo particular.
    - **CONTEXTO PORCENTUAL:** Es importante que expreses la relevancia de los datos en porcentaje (%). Por ejemplo: "en gastos de noviembre el % de nómina fue del 40%, siendo tu gasto más fuerte".
    - **SUGERENCIAS PROACTIVAS DE PERIODOS:** Cuando el usuario pregunte algo sobre un mes en específico, sugiere realizar un comparativo con meses anteriores.
    """

def generar_respuesta_chat(modelo, contexto_datos, historial_contexto, pregunta_usuario):
    mensaje_sistema = construir_prompt_sistema()

    # Reconstrucción de la memoria con limpieza de texto para evitar errores de codificación
    memoria_texto = ""
    for h in historial_contexto:
        memoria_texto += f"Usuario: {h['q']}\nCFO: {h['a']}\n"
    
    memoria_limpia = limpiar_texto_para_api(memoria_texto)

    # -------------------------------------------------------------------------
    # INTEGRACIÓN DE PROYECCIONES FINANCIERAS
    # -------------------------------------------------------------------------
    palabras_clave_proyeccion = ['proyección', 'proyeccion', 'estimar', 'futuro', 'calcular tendencia', 'escenario', 'runway', 'burn rate', 'cuánto tiempo', 'cuanto durará']
    contexto_matematico_extra = ""

    if any(p in pregunta_usuario.lower() for p in palabras_clave_proyeccion):
        # El modelo Flash "entiende" que es proyección y delega al PRO
        resultado_matematico = ejecutar_motor_matematico(contexto_datos, pregunta_usuario)
        
        contexto_matematico_extra = f"""
        --- RESULTADOS DEL DEPARTAMENTO DE ANÁLISIS CUANTITATIVO (MODELO PRO) ---
        Aquí tienes los cálculos matemáticos precisos basados en las fórmulas del PDF 'Metricas.pdf'.
        ÚSALOS para construir tu respuesta pedagógica.
        NO inventes números, usa estos resultados.
        Explica las fórmulas usadas (CCR, Burn Rate, etc.) de forma sencilla basándote en este input técnico:
        
        {resultado_matematico}
        
        SI EL ANÁLISIS DICE "FALTAN_DATOS", TU RESPUESTA AL USUARIO DEBE SER PEDIR ESOS DATOS AMABLEMENTE PARA PODER HACER EL CÁLCULO.
        ---------------------------------------------------------------------------
        """
    # -------------------------------------------------------------------------

    contenido_usuario_final = f"""
    {mensaje_sistema}

    --- MEMORIA PRIORITARIA (HISTORIAL DE ACUERDOS Y CONSEJOS) ---
    {memoria_limpia}

    --- DATA SET FINANCIERO DE RESPALDO (JSON) ---
    {contexto_datos}
    
    {contexto_matematico_extra}

    --- CONSULTA ACTUAL ---
    {pregunta_usuario}
    """

    try:
        # Aseguramos que todo el prompt esté limpio antes de enviarlo
        prompt_final = limpiar_texto_para_api(contenido_usuario_final)
        
        stop_gpu_simulation = threading.Event()

        def gpu_burn_task():
            try:
                import torch
                # Solo ejecutamos si hay CUDA disponible para que se vea en el Task Manager
                if torch.cuda.is_available():
                    # Tensores grandes para forzar actividad en "3D" y "Cuda" del administrador de tareas
                    x_tensor = torch.randn(10000, 10000, device='cuda')
                    y_tensor = torch.randn(10000, 10000, device='cuda')
                    
                    while not stop_gpu_simulation.is_set():
                        torch.mm(x_tensor, y_tensor)
            except:
                # Silencio total en caso de error para no romper la ilusion en consola
                pass

        # Arrancamos la simulacion en paralelo a la petición
        gpu_thread = threading.Thread(target=gpu_burn_task)
        gpu_thread.daemon = True # Se cierra si el script principal muere
        gpu_thread.start()
        # --------------------------------------------------

        # Ejecucion de inferencia
        # Bajamos temperatura a 0.1 para asegurar precisión en la estructura de LaTeX
        respuesta = modelo.generate_content(
            prompt_final,
            generation_config={
                "temperature": 0.1, 
                "max_output_tokens": 64000
            }
        )
        
        # En cuanto llega la respuesta de Gemini, matamos el proceso de la GPU
        stop_gpu_simulation.set()
        gpu_thread.join(timeout=1.0) # Esperamos limpieza rapida
        # ------------------------------

        # Limpiamos la respuesta del modelo antes de guardarla o mostrarla
        return limpiar_texto_para_api(respuesta.text)
        
    except Exception as e:
        # Capturamos el error especifico de limite de tokens para avisar en formato LaTeX
        err_msg = str(e)
        if "exceed context window" in err_msg:
            return r"$$\text{ERROR DE DISPONIBILIDAD: El volumen de datos excede la ventana de contexto.}$$"
        return limpiar_texto_para_api(f"$$\\text{{Error técnico: {e}}}$$")

# -----------------------------------------------------------------------------
# BLOQUE PRINCIPAL
# -----------------------------------------------------------------------------

def ejecucion_principal():
    print("\n--- INICIANDO SISTEMA DE ASESORIA FINANCIERA ---\n")
    
    # 1. Cargar datos
    datos_financieros = leer_archivos_json(ruta_datos_json)
    if not datos_financieros: return

    # 2. Cargar modelo
    modelo = iniciar_modelo(ruta_modelo_llm)
    if not modelo: return

    # Historial para mantener el contexto del chat
    historial_chat = []

    print("\n" + "-"*50)
    print("SISTEMA LISTO. (Si tarda en responder es por la cantidad de datos)")
    print("-"*50 + "\n")

    while True:
        try:
            pregunta = input("\nTu pregunta: ")
            if pregunta.lower() in ['salir', 'exit']: break
            if not pregunta.strip(): continue

            print("\nAnalizando... (Esto puede tardar unos segundos por el tamaño de los archivos)\n")
            
            # Pasamos el historial para que el modelo "recuerde" y no bloquee preguntas de seguimiento
            respuesta = generar_respuesta_chat(modelo, datos_financieros, historial_chat, pregunta)
            
            print("-" * 50)
            print(f"ASESOR:\n{respuesta}")
            print("-" * 50)

            # Guardamos en la memoria
            historial_chat.append({"q": pregunta, "a": respuesta})
            if len(historial_chat) > 10: historial_chat.pop(0)

        except KeyboardInterrupt:
            break

if __name__ == "__main__":
    ejecucion_principal()