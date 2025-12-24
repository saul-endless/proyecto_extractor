import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import os
import sys
from pathlib import Path
import urllib3
import time

# Se deshabilitan las advertencias de SSL para conexiones a traves de ngrok
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# -------------------------------------------------------------------------
# Se configura el modo de ejecucion
# 1 = Se inicia desde PDF (Flujo completo)
# 2 = Se inicia desde Categorizacion (Busca JSONs _MODIFICADO en input)
# 3 = Se inicia desde Perfilado (Busca JSONs _MODIFICADO_MODIFICADO en input)
FASE_INICIAL = 1
# -------------------------------------------------------------------------

# Se define la URL de conexion a la API
API_URL = "http://brandie-unsportsmanlike-fred.ngrok-free.dev"

# Se definen las rutas locales del sistema
BASE_PATH = Path("/Users/ivan/Library/Mobile Documents/com~apple~CloudDocs/ENDLESS INNOVATION TRABAJO/EXTRACCIÓN DE DATOS/CÓDIGO/PROYECTO EXTRACTOR")
DIR_INPUT = BASE_PATH / "input"
DIR_OUTPUT = BASE_PATH / "output"

# Se configura el intervalo de consulta en segundos
INTERVALO_POLLING = 10

# Funciones de utilidad

def crear_sesion_robusta():
    # Se crea una sesion HTTP con reintentos automaticos y configuracion para conexiones de larga duracion a traves de ngrok
    sesion = requests.Session()
    
    # Se configuran headers para mantener la conexion viva durante procesos largos
    sesion.headers.update({
        'Connection': 'keep-alive',
        'Keep-Alive': 'timeout=7200, max=100'
    })
    
    # Se configura la estrategia de reintentos para manejar errores de conexion transitorios
    estrategia_reintentos = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["POST", "GET"]
    )
    
    # Se aplica el adaptador con la estrategia de reintentos a ambos protocolos
    adaptador = HTTPAdapter(max_retries=estrategia_reintentos, pool_connections=1, pool_maxsize=1)
    sesion.mount("http://", adaptador)
    sesion.mount("https://", adaptador)
    
    return sesion

def confirmar_continuacion(mensaje):
    # Se solicita la confirmacion del usuario para continuar con el proceso
    respuesta = input(f"\n{mensaje} (s/n): ")
    if respuesta.lower() != 's':
        print("Se detiene el proceso por solicitud del usuario.")
        sys.exit()

def guardar_json_output(nombre, datos):
    # Se guarda el archivo JSON generado en el directorio de salida
    ruta_completa = DIR_OUTPUT / nombre
    with open(ruta_completa, 'w', encoding='utf-8') as f:
        json.dump(datos, f, indent=4, ensure_ascii=False)
    print(f"Archivo guardado en OUTPUT: {nombre}")

def buscar_archivos_input(patrones):
    # Se buscan archivos en el directorio de entrada que coincidan con los patrones dados
    encontrados = []
    if DIR_INPUT.exists():
        for patron in patrones:
            encontrados.extend(list(DIR_INPUT.glob(patron)))
    return encontrados

def esperar_resultado(sesion, job_id):
    # Se consulta el estado del trabajo periodicamente hasta que finalice
    print(f"Esperando resultado del trabajo {job_id}...")
    while True:
        try:
            response = sesion.get(f"{API_URL}/estado/{job_id}", timeout=30, verify=False)
            if response.status_code == 200:
                estado = response.json()
                status = estado.get("status")
                
                if status == "completado":
                    print("Trabajo completado exitosamente.")
                    return estado.get("resultado")
                elif status == "error":
                    print(f"Error en el servidor: {estado.get('error')}")
                    sys.exit()
                else:
                    print(f"Estado: {status}... esperando {INTERVALO_POLLING}s")
            else:
                print(f"Error consultando estado: {response.status_code}")
        except Exception as e:
            print(f"Error de conexion al consultar estado: {e}")
        
        time.sleep(INTERVALO_POLLING)

# Flujo principal

def main():
    # Se asegura la existencia del directorio de salida
    DIR_OUTPUT.mkdir(parents=True, exist_ok=True)
    
    # Se crea la sesion HTTP robusta para todas las peticiones
    sesion = crear_sesion_robusta()
    
    # Se inicializan las variables de estado para el seguimiento de archivos
    archivos_actuales_ingresos_egresos = [] 
    archivo_actual_datos = None 

    # ---------------------------------------------------------
    # FASE 1: EXTRACCION
    # ---------------------------------------------------------
    if FASE_INICIAL == 1:
        print("\nINICIO FASE 1: EXTRACCION DE DATOS")
        
        # Se buscan archivos PDF en el directorio de entrada
        archivos_pdf = buscar_archivos_input(["*.pdf"])
        if not archivos_pdf:
            print(f"Error: No hay PDF en {DIR_INPUT}")
            sys.exit()
            
        archivo_pdf = archivos_pdf[0]
        print(f"Procesando: {archivo_pdf.name}")

        confirmar_continuacion("Iniciar Fase 1 con este archivo?")

        try:
            # Se envia el archivo PDF a la API para su extraccion
            with open(archivo_pdf, 'rb') as f:
                files = {'file': (archivo_pdf.name, f, 'application/pdf')}
                response = sesion.post(f"{API_URL}/fase1/extraer", files=files, timeout=600, verify=False)
            
            if response.status_code != 200:
                print(f"Error Fase 1: {response.text}")
                sys.exit()
                
            datos_fase1 = response.json()
            
            # Se guardan los resultados obtenidos en el directorio de salida
            for key, info in datos_fase1.items():
                guardar_json_output(info['filename'], info['data'])
                
                # Se clasifican los archivos para su uso en la siguiente fase
                if "INGRESOS" in key or "EGRESOS" in key:
                    archivos_actuales_ingresos_egresos.append((info['filename'], info['data']))
                elif "DATOS" in key:
                    archivo_actual_datos = (info['filename'], info['data'])
                
        except Exception as e:
            print(f"Error conexion: {e}")
            sys.exit()

    # ---------------------------------------------------------
    # PREPARACION FASE 2
    # ---------------------------------------------------------
    if FASE_INICIAL <= 2:
        print("\nPREPARANDO FASE 2...")
        
        archivos_para_enviar_fase2 = [] 

        # Se verifica si se inicia directamente en Fase 2 para cargar archivos especificos
        if FASE_INICIAL == 2:
            print("Cargando archivos _MODIFICADO desde INPUT...")
            # Se buscan especificamente los archivos con sufijo _MODIFICADO
            rutas = buscar_archivos_input(["*_INGRESOS_MODIFICADO.json", "*_EGRESOS_MODIFICADO.json"])
            rutas_datos = buscar_archivos_input(["*_DATOS_MODIFICADO.json"])
            
            if not rutas:
                print("Error: No se encontraron archivos *_MODIFICADO.json en input para Fase 2")
                sys.exit()

            # Se cargan los archivos de transacciones y se preparan para el envio directo
            for r in rutas:
                with open(r, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # Se agrega a la lista de envio sin guardar copia en output
                    archivos_para_enviar_fase2.append((r.name, data))
            
            # Se carga el archivo de datos si existe
            if rutas_datos:
                with open(rutas_datos[0], 'r', encoding='utf-8') as f:
                    data_datos = json.load(f)
                    archivo_actual_datos = (rutas_datos[0].name, data_datos)

        else:
            # Se ejecuta la logica de simulacion si se viene de la Fase 1
            # Se generan versiones modificadas de los archivos originales
            nuevos_ingresos_egresos = []
            for nombre, datos in archivos_actuales_ingresos_egresos:
                nombre_modificado = nombre.replace(".json", "_MODIFICADO.json")
                guardar_json_output(nombre_modificado, datos)
                archivos_para_enviar_fase2.append((nombre_modificado, datos))
                nuevos_ingresos_egresos.append((nombre_modificado, datos))
            
            archivos_actuales_ingresos_egresos = nuevos_ingresos_egresos

            if archivo_actual_datos:
                nombre_datos, datos_datos = archivo_actual_datos
                nombre_datos_mod = nombre_datos.replace(".json", "_MODIFICADO.json")
                guardar_json_output(nombre_datos_mod, datos_datos)
                archivo_actual_datos = (nombre_datos_mod, datos_datos)

        # ---------------------------------------------------------
        # FASE 2: CATEGORIZACION
        # ---------------------------------------------------------
        print("\nINICIO FASE 2: CATEGORIZACION (GPU)")
        
        confirmar_continuacion("Iniciar Categorizacion en GPU?")
        
        files_payload = []
        for nombre_archivo, _ in archivos_para_enviar_fase2:
            # Se determina la ruta origen (Input o Output) para evitar duplicados
            ruta = DIR_OUTPUT / nombre_archivo
            if not ruta.exists():
                ruta = DIR_INPUT / nombre_archivo
            
            # Se preparan los archivos para el envio
            files_payload.append(('files', (nombre_archivo, open(ruta, 'rb'), 'application/json')))

        try:
            # Se realiza la peticion a la API para categorizar
            # Se envia y se obtiene el job_id inmediatamente
            response = sesion.post(
                f"{API_URL}/fase2/categorizar",
                files=files_payload,
                timeout=60,
                verify=False
            )
        except Exception as e:
            print(f"Error critico Fase 2: {e}")
            sys.exit()
        finally:
            # Se cierran los manejadores de archivos abiertos
            for _, file_tuple in files_payload:
                file_tuple[1].close()

        if response.status_code != 200:
            print(f"Error Fase 2: {response.text}")
            sys.exit()
        
        # Se obtiene el job_id y se espera el resultado mediante polling
        job_info = response.json()
        job_id = job_info.get("job_id")
        print(f"Trabajo iniciado con ID: {job_id}")
        
        # Se espera el resultado consultando periodicamente
        datos_fase2 = esperar_resultado(sesion, job_id)
            
        print("\nResultados Fase 2 recibidos.")
        
        archivos_actuales_ingresos_egresos = []
        # Se procesan y guardan los resultados de la categorizacion
        for nombre_orig_modificado, contenido in datos_fase2.items():
            nombre_con_giros = nombre_orig_modificado.replace(".json", "_CON_GIROS.json")
            guardar_json_output(nombre_con_giros, contenido)
            archivos_actuales_ingresos_egresos.append((nombre_con_giros, contenido))

    # ---------------------------------------------------------
    # PREPARACION FASE 3
    # ---------------------------------------------------------
    if FASE_INICIAL <= 3:
        print("\nPREPARANDO FASE 3...")
        
        archivos_para_enviar_fase3 = []

        # Se verifica si se inicia directamente en Fase 3 para cargar archivos especificos
        if FASE_INICIAL == 3:
             print("Cargando archivos complejos desde INPUT para Fase 3...")
             # Se buscan archivos con la nomenclatura especifica solicitada
             rutas_ingresos = buscar_archivos_input(["*_INGRESOS_MODIFICADO_CON_GIROS_MODIFICADO.json"])
             rutas_egresos = buscar_archivos_input(["*_EGRESOS_MODIFICADO_CON_GIROS_MODIFICADO.json"])
             rutas_datos = buscar_archivos_input(["*_DATOS_MODIFICADO_MODIFICADO.json"]) 
             
             rutas_todas = rutas_ingresos + rutas_egresos
             
             if not rutas_todas or not rutas_datos:
                 print("Error: Faltan archivos con nomenclatura _MODIFICADO_CON_GIROS_MODIFICADO o _DATOS_MODIFICADO_MODIFICADO en input.")
                 sys.exit()

             # Se cargan los archivos de transacciones tal cual estan
             for r in rutas_todas:
                 with open(r, 'r', encoding='utf-8') as f:
                     data = json.load(f)
                     # Se agrega a la lista de envio sin guardar copia en output
                     archivos_para_enviar_fase3.append((r.name, data))
             
             if rutas_datos:
                 with open(rutas_datos[0], 'r', encoding='utf-8') as f:
                     data = json.load(f)
                     archivo_actual_datos = (rutas_datos[0].name, data)
                     # Tambien se agrega el archivo de datos a la lista de envio
                     archivos_para_enviar_fase3.append((rutas_datos[0].name, data))

        else:
            # Se ejecuta la logica de simulacion si se viene de fases previas (1 o 2)
            # Se generan versiones modificadas nuevamente sobre los archivos con giros
            
            # Se procesan las transacciones
            for nombre, datos in archivos_actuales_ingresos_egresos:
                nombre_re_modificado = nombre.replace(".json", "_MODIFICADO.json")
                guardar_json_output(nombre_re_modificado, datos)
                archivos_para_enviar_fase3.append((nombre_re_modificado, datos))

            # Se procesan los datos generales
            if archivo_actual_datos:
                nombre_datos, datos_datos = archivo_actual_datos
                nombre_datos_re_mod = nombre_datos.replace(".json", "_MODIFICADO.json")
                guardar_json_output(nombre_datos_re_mod, datos_datos)
                archivos_para_enviar_fase3.append((nombre_datos_re_mod, datos_datos))
                archivo_actual_datos = (nombre_datos_re_mod, datos_datos)
            else:
                print("Error: Falta archivo DATOS para Fase 3")
                sys.exit()

        # ---------------------------------------------------------
        # FASE 3: PERFILADO
        # ---------------------------------------------------------
        print("\nINICIO FASE 3: PERFILADO EMPRESARIAL")
        
        confirmar_continuacion("Generar Perfil Empresarial?")
        
        files_payload = []
        # Se preparan todos los archivos requeridos para el perfilado
        for nombre_archivo, _ in archivos_para_enviar_fase3:
            # Se determina la ruta origen (Input o Output) para evitar duplicados
            ruta = DIR_OUTPUT / nombre_archivo
            if not ruta.exists():
                ruta = DIR_INPUT / nombre_archivo
                
            files_payload.append(('files', (nombre_archivo, open(ruta, 'rb'), 'application/json')))

        try:
            # Se envia la solicitud de perfilado a la API
            # Se envia y se obtiene el job_id inmediatamente
            response = sesion.post(
                f"{API_URL}/fase3/perfilar",
                files=files_payload,
                timeout=60,
                verify=False
            )
        except Exception as e:
            print(f"Error critico Fase 3: {e}")
            sys.exit()
        finally:
            # Se cierran los archivos
            for _, file_tuple in files_payload:
                file_tuple[1].close()

        if response.status_code != 200:
            print(f"Error Fase 3: {response.text}")
            sys.exit()
        
        # Se obtiene el job_id y se espera el resultado mediante polling
        job_info = response.json()
        job_id = job_info.get("job_id")
        print(f"Trabajo iniciado con ID: {job_id}")
        
        # Se espera el resultado consultando periodicamente
        perfil_final = esperar_resultado(sesion, job_id)
            
        print("\nPerfilado completado.")
        
        # Se construye el nombre final del perfil y se guarda
        nombre_base_datos = archivo_actual_datos[0] 
        nombre_final_perfil = nombre_base_datos.replace(".json", "_PERFIL.json")
        
        guardar_json_output(nombre_final_perfil, perfil_final)
        print("\nPROCESO COMPLETADO EXITOSAMENTE")

if __name__ == "__main__":
    main()