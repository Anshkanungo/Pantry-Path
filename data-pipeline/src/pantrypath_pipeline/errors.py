"""Pipeline errors. Messages should say what failed and what to check next."""


class PipelineError(Exception):
    """Base class: a step failed in a way we expected and can explain."""


class DataUnavailableError(PipelineError):
    """A source could not be reached, or returned nothing."""


class DataCorruptError(PipelineError):
    """A file exists but can't be read, or has the wrong shape."""


class ValidationFailedError(PipelineError):
    """Data failed a blocking quality check."""


class LLMError(PipelineError):
    """The LLM call failed or returned unusable output."""


class ProviderError(PipelineError):
    """A store API (Kroger, Target) failed."""
