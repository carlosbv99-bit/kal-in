"""
Excepciones de la capa epistémica.

Todas heredan de `EpistemicsError` para que un consumidor (kal-in, u otro
agente) pueda distinguir "esto es un problema de la capa epistémica" de un
error cualquiera del host.

Ninguna de estas excepciones se atrapa para seguir como si nada: si el ledger
no puede validar una hipótesis, la hipótesis no entra. Degradar en silencio
una violación estructural es exactamente cómo un sistema termina afirmando
cosas que no puede sostener.
"""
from __future__ import annotations


class EpistemicsError(Exception):
    """Base de todos los errores de esta capa."""


class SchemaVersionMismatch(EpistemicsError):
    """
    Los datos guardados pertenecen a otra versión del esquema.

    Se levanta en vez de "interpretar igual" a propósito: reinterpretar datos
    de procedencia con el esquema equivocado corrompe la trazabilidad en
    silencio, y la trazabilidad es lo único que este sistema tiene para
    ofrecer.
    """


class EstadoIlegible(EpistemicsError):
    """
    El estado guardado no se puede ni leer: JSON roto, fila corrupta, archivo
    truncado.

    Es distinto de `SchemaVersionMismatch`: ahí los datos se entienden pero son
    de otra versión; acá no se entienden. La respuesta es la misma —levantar—,
    pero confundir los dos diagnósticos manda a buscar el problema al lugar
    equivocado.
    """


class InvalidHypothesis(EpistemicsError):
    """
    La hipótesis viola una regla estructural — típicamente: se la quiere
    marcar como sostenida sin evidencia exógena, o sin declarar qué la
    refutaría.
    """


class UnknownId(EpistemicsError):
    """Se pidió algo por un id que el ledger no conoce."""


class DuplicateId(EpistemicsError):
    """Se intentó registrar un id ya existente con contenido distinto."""


class InvalidCondition(EpistemicsError):
    """
    Una `ReopenCondition` viola un tope de seguridad: demasiadas palabras
    clave, una vacía, o demasiadas fuentes declaradas.
    """


class LedgerLimitExceeded(EpistemicsError):
    """
    El ledger alcanzó uno de sus topes (hipótesis, condiciones, vínculos,
    evidencia, largo de un campo).

    Falla cerrado a propósito: un ledger sin topes, alimentado por el modelo,
    es un agotamiento de recursos trivial de provocar — y con una inyección de
    prompt, trivial de provocar a voluntad.
    """
