# Automatización de Reportes de Servicios

Aplicación de escritorio en Python para:

- seleccionar el reporte mensual correcto;
- capturar voz o texto sobre la jornada;
- redactar el bloque diario con IA;
- diligenciar automáticamente el Word correspondiente;
- crear tareas programadas para mostrar el popup al cierre de la jornada.

## Requisitos

1. Tener Microsoft Word instalado.
2. Tener Python 3.14 o compatible.
3. Configurar una clave `OPENAI_API_KEY` o pegarla en la configuración de la aplicación.

## Instalación rápida

```powershell
python -m pip install -r requirements.txt
python -m reporting_assistant.main
```

## Empaquetar a `.exe`

```powershell
powershell -ExecutionPolicy Bypass -File .\build_report_assistant.ps1
```

## Instalar popup diario

```powershell
powershell -ExecutionPolicy Bypass -File .\install_report_popup.ps1
```

El instalador crea dos tareas:

- lunes a miércoles a la hora definida en configuración;
- jueves y viernes a la hora definida en configuración.

## Flujo recomendado

1. Abra la app.
2. Seleccione el reporte.
3. Escoja la fecha.
4. Revise la jornada sugerida o cámbiela.
5. Dicte o escriba lo realizado.
6. Genere la vista previa.
7. Corrija si hace falta.
8. Guarde en Word.

## Notas

- Si el Word del mes ya existe, la app lo actualiza.
- Si no existe, crea uno desde la plantilla seleccionada.
- Si la fecha ya existe, pregunta si reemplaza o agrega.
- Las horas extra se guardan como un segundo tramo dentro del mismo bloque del día.
