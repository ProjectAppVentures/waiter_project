# Contexto actual · Waiter r2 · 2026-10-08

Nueva ronda desde main@367c4f7, con seis causas acotadas autorizadas y una QA conjunta. w1 implementa cookie e invitaciones; w2 exportación de clientes; w3 navegación de consolas. El orquestador conserva configuración, mapas y registros. La ronda anterior ya está integrada (#16–#19).

Cada sesión trabaja en su rama improve/ y worktree r2 propio. No tocar el clon principal ni ejecutar migraciones contra bases vivas. El cierre aprobado incluye los cuatro PR, QA combinada y merge-queue de esta ronda. Evidencia y pendientes en docs/audits/improvement-2026-10-08-waiter-r2/README.md.

## WT-001 · 26 de noviembre de 2026

La receta optativa de caja cerrada v1 prepara y coteja la copia ficticia para
M-01. Su entrada y su manifiesto están descritos en la guía técnica. La
validación automática vive en la ronda de CI. WT-002 parte de la integración
acreditada de WT-001 en `main`. La aceptación funcional sigue sujeta al encargo
autorizado de QA.

## WT-002 · 26 de noviembre de 2026

El modo QA del lanzador consume el perfil de WT-001, acredita la identidad
de Next y Django y limita arranque, reuso y parada al montaje propio. La
ronda de CI comprueba el ciclo de vida con recursos desechables. El montaje
de DevQA y la aceptación M-01 siguen reservados al encargo autorizado.
