"""Opt-in R&D skeletons. Import to register classes; inject backends to run them."""

from .architect import ArchitectAgent
from .coder import CoderAgent
from .reviewer import ReviewerAgent
from .tester import TesterAgent
from .interfaces import ArtifactGenerator, GenerationRequest, SandboxExecutor
from .schemas import ArchitectArtifacts, CoderArtifacts, ReviewerArtifacts, SandboxTestResult, TesterArtifacts

__all__ = ["ArchitectAgent", "CoderAgent", "ReviewerAgent", "TesterAgent", "ArtifactGenerator", "GenerationRequest",
           "SandboxExecutor", "ArchitectArtifacts", "CoderArtifacts", "ReviewerArtifacts", "SandboxTestResult", "TesterArtifacts"]
