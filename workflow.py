#!/usr/bin/env python3
"""
Flujo de trabajo: correos de clientes -> asistencia -> orden de trabajo.

Uso:
    python workflow.py init                    # prepara data/ y config
    python workflow.py check                   # valida configuración y conexión
    python workflow.py once                    # ejecuta un ciclo y termina
    python workflow.py run                     # ciclo continuo (servicio)
    python workflow.py estado                  # muestra los trabajos abiertos
    python workflow.py confirmar T01 --si      # registra asistencia a mano
    python workflow.py simular correo.txt      # prueba el flujo sin tocar el buzón
    python workflow.py webhook --puerto 8080   # recibe respuestas de WhatsApp
    python workflow.py inventario              # muestra existencias y faltantes
    python workflow.py completar JB-...        # cierra el trabajo y descuenta material
    python workflow.py exportar --ver          # control de trabajos para Excel
"""

import argparse
import logging
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.workflow.config import load_config, validate_config
from src.workflow.inbox import WhatsAppInbox
from src.workflow.inventory import Inventory
from src.workflow.models import EstadoTrabajo
from src.workflow.runner import WorkflowRunner
from src.workflow.store import WorkflowStore

logger = logging.getLogger("workflow")


def setup_logging(config: dict, verbose: bool = False) -> None:
    log_config = config.get("logging", {})
    nivel = logging.DEBUG if verbose else getattr(
        logging, str(log_config.get("level", "INFO")).upper(), logging.INFO
    )
    archivo = log_config.get("file", "logs/workflow.log")
    Path(archivo).parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=nivel,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(archivo)],
    )


# --- comandos ---------------------------------------------------------------

def cmd_init(args, config: dict) -> int:
    """Create the working files from the shipped examples."""
    rutas = config.get("rutas", {})
    for carpeta in ("data", "logs"):
        Path(carpeta).mkdir(parents=True, exist_ok=True)

    copiados = []
    for destino_key, ejemplo in (
        ("inventario", "data/inventario.ejemplo.csv"),
        ("trabajadores", "data/trabajadores.ejemplo.csv"),
        ("propiedades", "data/propiedades.ejemplo.csv"),
        ("unidades", "data/unidades.ejemplo.csv"),
        ("consumos", "data/consumos.ejemplo.csv"),
    ):
        destino = Path(rutas.get(destino_key, f"data/{destino_key}.csv"))
        if destino.exists():
            print(f"  ya existe, no se toca: {destino}")
            continue
        if not Path(ejemplo).exists():
            print(f"  falta la plantilla {ejemplo}")
            continue
        shutil.copy(ejemplo, destino)
        copiados.append(str(destino))
        print(f"  creado: {destino}")

    print("\nListo. Ahora:")
    print("  1. data/propiedades.csv  -> tus empresas de gestión y sus complejos")
    print("     (el 'dominio' o 'contacto' es lo que identifica de quién es el correo)")
    print("  2. data/trabajadores.csv -> tu personal, con habilidades y teléfono")
    print("  3. data/inventario.csv   -> tus existencias")
    print("  4. data/unidades.csv     -> tamaño de cada apartamento (1+1, 3+2)")
    print("  5. data/consumos.csv     -> material que se lleva por servicio y tamaño")
    print("  6. Copia .env.example a .env y llena las credenciales del correo.")
    print("  7. Corre: python workflow.py check")
    return 0


def cmd_check(args, config: dict) -> int:
    """Validate config and test the mailbox connection."""
    problemas = validate_config(config)
    if problemas:
        print("Problemas de configuración:")
        for problema in problemas:
            print(f"  - {problema}")
    else:
        print("Configuración: OK")

    rutas = config.get("rutas", {})
    inventario = Inventory(rutas.get("inventario", "data/inventario.csv"))
    print(f"Inventario: {len(inventario.articulos)} artículos")

    runner = WorkflowRunner(config, dry_run=True)
    print(f"Trabajadores: {len(runner.roster.trabajadores)} "
          f"({sum(1 for t in runner.roster.trabajadores.values() if t.activo)} activos)")
    print(f"WhatsApp: proveedor '{config.get('whatsapp', {}).get('proveedor', 'manual')}'")

    if problemas:
        print("\nNo se prueba la conexión al correo hasta que la configuración esté completa.")
        return 1

    print("\nProbando conexión IMAP...")
    correos = runner.email.fetch_unread()
    print(f"Conexión OK. Correos sin leer en la bandeja: {len(correos)}")
    return 0


def cmd_once(args, config: dict) -> int:
    runner = WorkflowRunner(config, dry_run=args.dry_run)
    resumen = runner.run_once()
    print("\nResumen del ciclo:")
    for clave, valor in resumen.items():
        print(f"  {clave.replace('_', ' ')}: {valor}")
    return 0


def cmd_run(args, config: dict) -> int:
    runner = WorkflowRunner(config, dry_run=args.dry_run)
    runner.run_forever()
    return 0


def cmd_estado(args, config: dict) -> int:
    store = WorkflowStore(config.get("rutas", {}).get("estado", "data/workflow_state.json"))
    trabajos = list(store.trabajos.values())

    if args.job:
        trabajo = store.get(args.job.upper())
        if not trabajo:
            print(f"No existe el trabajo {args.job}")
            return 1
        _imprimir_detalle(trabajo)
        return 0

    if not args.todos:
        trabajos = [t for t in trabajos
                    if t.estado in (EstadoTrabajo.NUEVO.value,
                                    EstadoTrabajo.CONVOCANDO.value,
                                    EstadoTrabajo.SIN_PERSONAL.value)]

    if not trabajos:
        print("No hay trabajos que mostrar.")
        return 0

    trabajos.sort(key=lambda t: (t.fecha_servicio or "9999", t.propiedad, t.unidad))
    print(f"{'FOLIO':<18} {'ESTADO':<13} {'CONF':>5}  {'FECHA':<11} "
          f"{'SERVICIO':<8} TRABAJO")
    print("-" * 92)
    for trabajo in trabajos:
        conf = f"{len(trabajo.confirmados())}/{trabajo.trabajadores_requeridos}"
        print(f"{trabajo.id:<18} {trabajo.estado:<13} {conf:>5}  "
              f"{(trabajo.fecha_servicio or '-'):<11} "
              f"{(trabajo.servicio or '-'):<8} {trabajo.etiqueta()[:38]}")
    return 0


def _imprimir_detalle(trabajo) -> None:
    print(f"Folio:        {trabajo.id}")
    print(f"Estado:       {trabajo.estado}")
    print(f"Empresa:      {trabajo.empresa_gestion or '-'}")
    print(f"Propiedad:    {trabajo.propiedad or '-'}")
    print(f"Unidad:       {trabajo.unidad or '-'}  "
          f"({trabajo.tamano or 'tamaño desconocido'}, "
          f"{'ocupada' if trabajo.ocupada else 'vacante'})")
    print(f"Servicio:     {trabajo.servicio or '-'} - {trabajo.descripcion_servicio}")
    print(f"Cliente:      {trabajo.cliente_nombre} <{trabajo.cliente_email}>")
    print(f"Fecha:        {trabajo.fecha_servicio} {trabajo.turno or trabajo.hora_servicio}")
    print(f"Dirección:    {trabajo.direccion}")
    print(f"Habilidades:  {', '.join(trabajo.habilidades) or 'cualquiera'}")
    print(f"Requeridos:   {trabajo.trabajadores_requeridos}")
    print(f"\nDescripción:\n{trabajo.descripcion}")

    print("\nMaterial:")
    for item in trabajo.items or []:
        faltante = f"  (FALTAN {item.faltante})" if item.faltante > 0 else ""
        print(f"  - {item}{faltante}")
    if not trabajo.items:
        print("  (sin material)")

    print("\nAsistencia:")
    for convocatoria in trabajo.convocatorias.values():
        print(f"  {convocatoria.trabajador_id:<6} {convocatoria.nombre:<22} "
              f"{convocatoria.estado:<12} {convocatoria.canal}")
    if not trabajo.convocatorias:
        print("  (nadie convocado todavía)")

    if trabajo.notas:
        print("\nHistorial:")
        for nota in trabajo.notas[-15:]:
            print(f"  {nota}")


def cmd_confirmar(args, config: dict) -> int:
    """Queue a manual attendance answer; the next cycle applies it."""
    if args.si == args.no:
        print("Indica --si o --no (uno de los dos).")
        return 1

    runner = WorkflowRunner(config, dry_run=args.dry_run)
    trabajador = (runner.roster.get(args.trabajador.upper())
                  or runner.roster.by_phone(args.trabajador))
    if not trabajador:
        print(f"No encuentro al trabajador '{args.trabajador}' en data/trabajadores.csv")
        return 1

    respuesta = "SI" if args.si else "NO"
    job_id = args.job.upper() if args.job else None
    texto = f"{respuesta} {job_id}" if job_id else respuesta

    inbox = WhatsAppInbox(config.get("rutas", {}).get("whatsapp_inbox",
                                                      "data/whatsapp_inbox.jsonl"))
    inbox.append(trabajador.telefono, texto, origen="manual", job_id=job_id)
    print(f"Registrado: {trabajador.nombre} -> {respuesta}"
          f"{' para ' + job_id if job_id else ' (trabajo pendiente más reciente)'}")

    resumen = runner.run_once()
    print(f"Ciclo ejecutado: {resumen['respuestas']} respuestas procesadas, "
          f"{resumen['trabajos_asignados']} trabajos asignados")
    return 0


def cmd_simular(args, config: dict) -> int:
    """Feed a text file to the parser as if it arrived by email (no IMAP, no sending)."""
    ruta = Path(args.archivo)
    if not ruta.exists():
        print(f"No existe el archivo {ruta}")
        return 1

    cuerpo = ruta.read_text(encoding="utf-8")
    runner = WorkflowRunner(config, dry_run=not args.enviar)

    # Un correo de empresa de gestión se lee renglón por renglón, no como una
    # sola solicitud, así que la vista previa tiene que mostrarlo igual.
    propiedad = runner.propiedades.match(args.de, cuerpo, args.asunto)
    if propiedad:
        _previsualizar_unidades(runner, propiedad, cuerpo)
    else:
        _previsualizar_solicitud(runner, args, cuerpo)

    if not args.crear:
        print("\n(No se creó nada. Usa --crear para registrarlo de verdad.)")
        return 0

    from src.workflow.email_client import CorreoEntrante
    correo = CorreoEntrante(
        uid="simulado", message_id=f"<simulado-{ruta.stem}>",
        remitente_nombre="", remitente_email=args.de,
        asunto=args.asunto, cuerpo=cuerpo,
    )
    resumen = {"trabajos_nuevos": 0}
    runner._crear_trabajo(correo, resumen)
    runner._atender_trabajos({"convocatorias_enviadas": 0, "recordatorios": 0,
                              "trabajos_asignados": 0, "trabajos_sin_personal": 0})
    runner.store.save()
    print(f"\n{resumen['trabajos_nuevos']} trabajos creados y convocatorias "
          f"{'enviadas' if args.enviar else 'simuladas'}.")
    return 0


def _previsualizar_unidades(runner, propiedad, cuerpo: str) -> None:
    """Preview of a management-company email: one row per unit."""
    unidades = runner.property_parser.parse(cuerpo)
    print(f"Empresa de gestión: {propiedad.empresa_gestion or '(sin definir)'}")
    print(f"Propiedad:          {propiedad.propiedad}")
    print(f"Unidades detectadas: {len(unidades)}\n")

    if not unidades:
        print("  Ningún renglón parece una unidad. Revisa el correo a mano.")
        return

    print(f"{'UNIDAD':<12} {'SERVICIO':<9} {'FECHA':<11} {'TURNO':<6} "
          f"{'TAMAÑO':<7} DESCRIPCIÓN / MATERIAL")
    print("-" * 96)
    for unidad in unidades:
        tamano = unidad.tamano or runner.tamanos.get(propiedad.propiedad, unidad.unidad)
        items = runner.consumos.para(unidad.servicio, tamano)
        runner.inventory.check(items)
        material = ", ".join(
            f"{i.sku} x{i.cantidad:g}" + ("!" if i.faltante else "") for i in items
        ) or "(sin regla en consumos.csv)"
        print(f"{unidad.unidad:<12} {unidad.servicio:<9} "
              f"{(unidad.fecha or '-'):<11} {(unidad.turno or '-'):<6} "
              f"{(tamano or '?'):<7} {unidad.descripcion} | {material}")
    print("\n! = no alcanza el inventario")


def _previsualizar_solicitud(runner, args, cuerpo: str) -> None:
    """Preview of a one-off request (website form or direct client)."""
    campos = runner.parser.parse(args.asunto, cuerpo,
                                 remitente_email=args.de, remitente_nombre="")
    runner.inventory.check(campos["items"])

    print("Remitente no reconocido como empresa de gestión; "
          "se interpreta como solicitud suelta.\n")
    print("Interpretación del correo:")
    for clave, valor in campos.items():
        if clave == "items":
            print("  materiales:")
            for item in valor:
                estado = "OK" if item.faltante == 0 else f"FALTAN {item.faltante}"
                print(f"    - {item}  [{item.sku or 'sin sku'}: {estado}]")
        else:
            print(f"  {clave}: {valor}")


def cmd_webhook(args, config: dict) -> int:
    from src.workflow.webhook import run_webhook

    whatsapp = config.get("whatsapp", {})
    token = whatsapp.get("meta", {}).get("verify_token", "")
    run_webhook(
        puerto=args.puerto,
        host=args.host,
        inbox_path=config.get("rutas", {}).get("whatsapp_inbox",
                                               "data/whatsapp_inbox.jsonl"),
        verify_token=token,
    )
    return 0


def cmd_inventario(args, config: dict) -> int:
    inventario = Inventory(config.get("rutas", {}).get("inventario", "data/inventario.csv"))
    articulos = list(inventario.articulos.values())
    if args.bajos:
        articulos = inventario.bajo_minimo()
        if not articulos:
            print("Nada por debajo del mínimo.")
            return 0

    print(f"{'SKU':<14} {'ARTÍCULO':<34} {'UNID':<8} {'STOCK':>7} {'RESERV':>7} {'DISPON':>7}")
    print("-" * 82)
    for articulo in sorted(articulos, key=lambda a: a.nombre):
        marca = " !" if articulo.bajo_minimo else ""
        print(f"{articulo.sku:<14} {articulo.nombre[:34]:<34} {articulo.unidad:<8} "
              f"{articulo.stock:>7g} {articulo.reservado:>7g} {articulo.disponible:>7g}{marca}")
    print("\n! = en o por debajo del mínimo")
    return 0


def cmd_exportar(args, config: dict) -> int:
    """Export jobs with the columns of the tracking spreadsheet."""
    from src.workflow import export

    rutas = config.get("rutas", {})
    store = WorkflowStore(rutas.get("estado", "data/workflow_state.json"))
    trabajos = export.seleccionar(
        store.trabajos.values(),
        desde=args.desde or "", hasta=args.hasta or "",
        solo_asignados=args.solo_asignados,
    )

    if args.ver:
        print(export.tabla_texto(trabajos))
        return 0

    destino = args.salida or rutas.get("exportacion", "data/control_trabajos.csv")
    filas = export.escribir_csv(trabajos, destino, incluir_extra=args.detalle)
    print(f"{filas} filas exportadas a {destino}")
    print("Se abre en Excel y las columnas van en el mismo orden que tu hoja.")
    return 0


def cmd_completar(args, config: dict) -> int:
    """Close a job: the reserved material physically left the warehouse."""
    rutas = config.get("rutas", {})
    store = WorkflowStore(rutas.get("estado", "data/workflow_state.json"))
    trabajo = store.get(args.job.upper())
    if not trabajo:
        print(f"No existe el trabajo {args.job}")
        return 1

    if trabajo.estado == EstadoTrabajo.COMPLETADO.value:
        print(f"El trabajo {trabajo.id} ya estaba cerrado.")
        return 0

    if trabajo.inventario_reservado:
        Inventory(rutas.get("inventario", "data/inventario.csv")).consumir(trabajo.items)
        trabajo.inventario_reservado = False
        print("Material descontado del inventario.")

    trabajo.agregar_nota("Trabajo completado")
    trabajo.estado = EstadoTrabajo.COMPLETADO.value
    store.save()
    print(f"Trabajo {trabajo.id} cerrado.")
    return 0


# --- CLI --------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Flujo de correos de clientes, asistencia y órdenes de trabajo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default="config/workflow.yaml",
                        help="ruta al archivo de configuración")
    parser.add_argument("-v", "--verbose", action="store_true", help="log detallado")
    sub = parser.add_subparsers(dest="comando", required=True)

    sub.add_parser("init", help="crea data/inventario.csv y data/trabajadores.csv")
    sub.add_parser("check", help="valida la configuración y prueba el correo")

    p_once = sub.add_parser("once", help="ejecuta un ciclo y termina")
    p_once.add_argument("--dry-run", action="store_true",
                        help="no envía nada, solo muestra lo que enviaría")

    p_run = sub.add_parser("run", help="ejecuta el flujo en bucle")
    p_run.add_argument("--dry-run", action="store_true", help="no envía nada")

    p_estado = sub.add_parser("estado", help="muestra los trabajos")
    p_estado.add_argument("--job", help="detalle de un folio (JB-YYYYMMDD-NNN)")
    p_estado.add_argument("--todos", action="store_true",
                          help="incluye los ya asignados y cancelados")

    p_conf = sub.add_parser("confirmar", help="registra una respuesta a mano")
    p_conf.add_argument("trabajador", help="id (T01) o teléfono del trabajador")
    p_conf.add_argument("--job", help="folio; por defecto, su convocatoria más reciente")
    p_conf.add_argument("--si", action="store_true", help="confirma asistencia")
    p_conf.add_argument("--no", action="store_true", help="rechaza")
    p_conf.add_argument("--dry-run", action="store_true", help="no envía la orden de trabajo")

    p_sim = sub.add_parser("simular", help="prueba el parser con un archivo de texto")
    p_sim.add_argument("archivo", help="archivo con el cuerpo del correo")
    p_sim.add_argument("--asunto", default="Solicitud de servicio")
    p_sim.add_argument("--de", default="cliente@ejemplo.com")
    p_sim.add_argument("--crear", action="store_true", help="registra el trabajo de verdad")
    p_sim.add_argument("--enviar", action="store_true",
                       help="con --crear, envía los mensajes reales")

    p_web = sub.add_parser("webhook", help="servidor para respuestas de WhatsApp")
    p_web.add_argument("--puerto", type=int, default=8080)
    p_web.add_argument("--host", default="0.0.0.0")

    p_inv = sub.add_parser("inventario", help="muestra las existencias")
    p_inv.add_argument("--bajos", action="store_true", help="solo lo que está bajo mínimo")

    p_comp = sub.add_parser("completar", help="cierra un trabajo y descuenta el material")
    p_comp.add_argument("job", help="folio del trabajo")

    p_exp = sub.add_parser("exportar", help="genera el control de trabajos para Excel")
    p_exp.add_argument("--desde", help="fecha de servicio mínima (AAAA-MM-DD)")
    p_exp.add_argument("--hasta", help="fecha de servicio máxima (AAAA-MM-DD)")
    p_exp.add_argument("--salida", help="archivo CSV de salida")
    p_exp.add_argument("--solo-asignados", action="store_true",
                       help="solo los que ya tienen personal confirmado")
    p_exp.add_argument("--detalle", action="store_true",
                       help="agrega folio, estado, turno y material")
    p_exp.add_argument("--ver", action="store_true",
                       help="muestra la tabla en pantalla en vez de escribir el archivo")

    return parser


COMANDOS = {
    "init": cmd_init,
    "check": cmd_check,
    "once": cmd_once,
    "run": cmd_run,
    "estado": cmd_estado,
    "confirmar": cmd_confirmar,
    "simular": cmd_simular,
    "webhook": cmd_webhook,
    "inventario": cmd_inventario,
    "completar": cmd_completar,
    "exportar": cmd_exportar,
}


def main() -> int:
    load_dotenv()
    args = build_parser().parse_args()

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1

    setup_logging(config, verbose=args.verbose)
    return COMANDOS[args.comando](args, config)


if __name__ == "__main__":
    sys.exit(main())
