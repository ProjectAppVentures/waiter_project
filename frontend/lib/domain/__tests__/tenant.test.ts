import { centralHost, destinationOrigin, isCentralHost, isLocalHost, isPlatformHost, orgFromHost } from '@/lib/domain/tenant'

// Falla si la dupla se confunde con una organización o los pases cambian de host, esquema o puerto.
it('resuelve el destino del pase en el montaje QA optativo', () => {
  const qa = jest.replaceProperty(process, 'env', {
    ...process.env, NODE_ENV: 'development', NEXT_PUBLIC_WAITER_QA_CENTRAL_HOST: 'qa-a.waiter.localhost',
  })
  try {
    const central = 'qa-a.waiter.localhost:3117'
    expect(orgFromHost(central, 'demo')).toBeNull()
    expect(orgFromHost(`perfil-a.${central}`)).toBe('perfil-a')
    expect(orgFromHost(`platform.${central}`, 'demo')).toBeNull()
    expect(orgFromHost(`menu.${central}`, 'demo')).toBeNull()
    expect(isPlatformHost(`platform.${central}`)).toBe(true)
    expect(isPlatformHost('platform.qa-b.waiter.localhost:3117')).toBe(false)
    expect(centralHost(`perfil-a.${central}`)).toBe(central)
    expect(centralHost(`platform.${central}`)).toBe(central)
    expect(centralHost(`menu.${central}`)).toBe(central)
    expect(destinationOrigin('pos', 'perfil-b', { protocol: 'https:', host: central })).toBe(`https://perfil-b.${central}`)
    expect(destinationOrigin('platform', null, { protocol: 'http:', host: central })).toBe(`http://platform.${central}`)
  } finally {
    qa.restore()
  }
})

// Falla si el subdominio no se lee como la organización, si un nombre reservado (el de la consola, `platform`, o su
// nombre viejo, `plataforma`) o el dominio base se toman por una, si una IP o localhost a secas no caen en la
// organización por omisión (plan T), o si la consola deja de reconocerse en `platform.` o se sigue reconociendo en
// `plataforma.`.
it('saca la organización del subdominio', () => {
  expect(orgFromHost('burger-house.waiter.projectapp.co')).toBe('burger-house')
  expect(orgFromHost('burger-house.localhost:3000')).toBe('burger-house')
  expect(orgFromHost('waiter.projectapp.co')).toBeNull()
  expect(orgFromHost('platform.waiter.projectapp.co', 'burger-house')).toBe('burger-house')
  expect(orgFromHost('plataforma.waiter.projectapp.co', 'burger-house')).toBe('burger-house')
  expect(orgFromHost('192.168.56.10:3000', 'burger-house')).toBe('burger-house')
  expect(orgFromHost('localhost:3000')).toBeNull()
  expect(orgFromHost('Frisby.localhost:3000')).toBe('frisby')
  expect(orgFromHost('bad_slug.localhost:3000')).toBeNull()
  expect(isPlatformHost('platform.waiter.projectapp.co')).toBe(true)
  expect(isPlatformHost('plataforma.waiter.projectapp.co')).toBe(false)
  expect(isPlatformHost('burger-house.localhost:3000')).toBe(false)
})

// Falla si desde el subdominio de una organización o la consola (`platform.`) no se encuentra la dirección del inicio
// universal, o si una organización se confunde con él.
it('encuentra la dirección del inicio universal', () => {
  expect(centralHost('frisby.waiter.projectapp.co')).toBe('waiter.projectapp.co')
  expect(centralHost('platform.waiter.projectapp.co')).toBe('waiter.projectapp.co')
  expect(centralHost('frisby.localhost:3000')).toBe('localhost:3000')
  expect(centralHost('waiter.projectapp.co')).toBe('waiter.projectapp.co')
  expect(centralHost('192.168.56.10:3000')).toBe('192.168.56.10:3000')
  expect(isCentralHost('waiter.projectapp.co')).toBe(true)
  expect(isCentralHost('localhost:3000')).toBe(true)
  expect(isCentralHost('frisby.waiter.projectapp.co')).toBe(false)
  expect(isCentralHost('platform.waiter.projectapp.co')).toBe(false)
  expect(isLocalHost('192.168.56.10:3000') && isLocalHost('localhost:3000')).toBe(true)
  expect(isLocalHost('frisby.localhost:3000')).toBe(false)
})

// Falla si el pase va a una dirección equivocada: otra organización, la consola fuera de su subdominio en producción, o
// un subdominio de una IP (que no existe) en desarrollo.
it('elige la dirección donde se cambia el pase', () => {
  const prod = { protocol: 'https:', host: 'waiter.projectapp.co' }
  expect(destinationOrigin('pos', 'frisby', prod)).toBe('https://frisby.waiter.projectapp.co')
  expect(destinationOrigin('platform', null, prod)).toBe('https://platform.waiter.projectapp.co')
  expect(destinationOrigin('pos', 'frisby', { protocol: 'https:', host: 'zeta.waiter.projectapp.co' })).toBe('https://frisby.waiter.projectapp.co')
  const dev = { protocol: 'http:', host: 'localhost:3000' }
  expect(destinationOrigin('pos', 'frisby', dev, 'burger-house')).toBe('http://frisby.localhost:3000')
  expect(destinationOrigin('pos', 'burger-house', dev, 'burger-house')).toBe('http://localhost:3000')
  expect(destinationOrigin('platform', null, dev)).toBe('http://localhost:3000')
  const lan = { protocol: 'http:', host: '192.168.56.10:3000' }
  expect(destinationOrigin('pos', 'burger-house', lan, 'burger-house')).toBe('http://192.168.56.10:3000')
  expect(destinationOrigin('pos', 'frisby', lan, 'burger-house')).toBe('http://frisby.localhost:3000')
})
