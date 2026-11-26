import { createHash } from 'node:crypto'
import { realpathSync } from 'node:fs'
import { resolve } from 'node:path'
import { NextResponse, type NextRequest } from 'next/server'

import { isLocalHost, isPlatformHost, orgFromHost, qaCentralName, qaHost } from '@/lib/domain/tenant'
import { SITIO, type Sitio } from '@/lib/sitio'

// Waiter es una sola app de Next que sirve tres sitios según el host:
// - menu.<dominio> es el comensal. Sus páginas, su /sw.js, sus /icons y su manifiesto viven bajo el prefijo interno
//   /diner; la URL del navegador no cambia, así que los QR y NFC impresos y el service worker instalado siguen igual.
// - El dominio central con nombre (waiter.projectapp.co) muestra la landing en sus cuatro páginas; lo demás es del POS
//   (/login, /entrar…).
// - Todo lo demás es el POS, como antes: las organizaciones por subdominio, la consola (platform.), las IP y
//   «localhost» del desarrollo y de las tabletas de la LAN, y cualquier otro host, con la misma lógica de tenant.
// El proxy sólo decide y anota el sitio en el encabezado SITIO; la reescritura al prefijo la hace next.config.ts.
const LANDING = new Set(['/', '/privacy', '/terms', '/data-deletion'])
// Lo que el comensal comparte con el resto de la app: los archivos de Next, su API (el rewrite a experience) y los
// recursos públicos que no chocan con los del POS.
const COMPARTIDO = ['/_next/', '/api/', '/smart-menu/', '/mapas/']
// Los prefijos internos sólo se alcanzan por la reescritura, nunca pidiéndolos directo: si no, el comensal y la landing
// aparecerían también bajo los hosts del POS.
const INTERNO = /^\/(diner|landing)(\/|$)/
// La consola de ProjectApp pasó de /plataforma a /platform el 17-10-2026: un enlace o un marcador viejo llega a la misma
// pantalla en la ruta nueva, con su consulta. En el host del menú /plataforma sigue siendo del comensal.
const CONSOLA_VIEJA = /^\/plataforma(?=\/|$)/
const IDENTITY_PATH = '/__waiter_qa/identity'

function readQaIdentity() {
  if (!qaCentralName() || !process.env.WAITER_QA_RUNTIME) return null
  try {
    const { component, sha, checkout, mount_id, boot_id } = JSON.parse(process.env.WAITER_QA_RUNTIME)
    if (component !== 'frontend' || checkout !== realpathSync(resolve(process.cwd(), '..'))
      || typeof sha !== 'string' || !/^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(sha)
      || typeof boot_id !== 'string' || !/^[a-f0-9]{32}$/.test(boot_id)
      || typeof mount_id !== 'string' || mount_id.length > 32 || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(mount_id)) return null
    return { component, sha, mount_id, boot_id, checkout_digest: createHash('sha256').update(checkout).digest('hex') }
  } catch {
    return null
  }
}

const qaIdentity = readQaIdentity()

export const esHostDelMenu = (host: string): boolean => {
  const qa = qaCentralName()
  if (qa) return qaHost(host, qa)?.kind === 'menu'
  const labels = host.split(':')[0].toLowerCase().split('.')
  return labels.length >= 2 && labels[0] === 'menu'
}

// El host viejo de la consola (plataforma.) dejó de existir el 17-10-2026. Si algún servidor todavía lo manda acá,
// responde 404: si no, pasaría por dominio central, con la landing y un inicio de sesión que lleva el pase a
// platform.plataforma….
const esHostViejoDeLaConsola = (host: string): boolean => host.split(':')[0].toLowerCase().startsWith('plataforma.')

// El dominio central con nombre: sin organización, sin la consola y sin el menú. Una IP o «localhost» a secas siguen
// siendo del POS (el desarrollo y las tabletas de la LAN entran por ahí).
export const esCentral = (host: string): boolean => {
  const qa = qaCentralName()
  if (qa) return qaHost(host, qa)?.kind === 'central'
  return !isLocalHost(host) && !orgFromHost(host) && !isPlatformHost(host) && !esHostDelMenu(host)
}

export type Decision = { tipo: 'pasar' } | { tipo: 'no-encontrado' } | { tipo: 'sitio'; sitio: Sitio } | { tipo: 'redirigir'; ruta: string }

export function decidir(host: string, pathname: string, desarrollo = false): Decision {
  const qa = qaCentralName()
  if (pathname === IDENTITY_PATH || (qa && !qaHost(host, qa))) return { tipo: 'no-encontrado' }
  // La galería del kit es una herramienta local: el servidor publicado no sirve esta ruta.
  if (!desarrollo && (pathname === '/kit' || pathname.startsWith('/kit/'))) return { tipo: 'no-encontrado' }
  if (INTERNO.test(pathname)) return { tipo: 'no-encontrado' }
  if (esHostViejoDeLaConsola(host)) return { tipo: 'no-encontrado' }
  if (esHostDelMenu(host)) {
    return COMPARTIDO.some((prefijo) => pathname.startsWith(prefijo)) ? { tipo: 'pasar' } : { tipo: 'sitio', sitio: 'diner' }
  }
  if (CONSOLA_VIEJA.test(pathname)) return { tipo: 'redirigir', ruta: pathname.replace(CONSOLA_VIEJA, '/platform') }
  const limpio = pathname.length > 1 && pathname.endsWith('/') ? pathname.slice(0, -1) : pathname
  if (esCentral(host) && LANDING.has(limpio)) return { tipo: 'sitio', sitio: 'landing' }
  return { tipo: 'pasar' }
}

// Corre en todas las peticiones (sin matcher), también en /_next/static, para que ninguna llegue a las reescrituras de
// next.config.ts con un SITIO puesto por el cliente.
export function proxy(request: NextRequest) {
  const host = request.headers.get('host') ?? ''
  const qa = qaCentralName()
  if (qa && qaIdentity && request.method === 'GET' && request.nextUrl.pathname === IDENTITY_PATH && qaHost(host, qa)) {
    return NextResponse.json(qaIdentity, { headers: { 'Cache-Control': 'no-store' } })
  }
  const decision = decidir(host, request.nextUrl.pathname, process.env.NODE_ENV === 'development')
  if (decision.tipo === 'no-encontrado') {
    return new NextResponse('Not Found', { status: 404, headers: { 'Content-Type': 'text/plain; charset=utf-8' } })
  }
  if (decision.tipo === 'redirigir') {
    // Next deja relativa la dirección de una redirección al mismo host: el navegador la resuelve contra la dirección
    // pública, detrás de nginx también.
    const destino = request.nextUrl.clone()
    destino.pathname = decision.ruta
    return NextResponse.redirect(destino, 308)
  }
  // El sitio lo anota sólo este proxy: el encabezado que mande el cliente se descarta en cada petición.
  const headers = new Headers(request.headers)
  headers.delete(SITIO)
  if (decision.tipo === 'sitio') headers.set(SITIO, decision.sitio)
  return NextResponse.next({ request: { headers } })
}
