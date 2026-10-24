"""A small local document collection standing in for the web and a vector store.

Most documents are about RAG evaluation; a few are deliberately off-topic so a
broken retriever has something irrelevant to return.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    text: str
    url: str


DOCUMENTS: tuple[Document, ...] = (
    Document(
        "rag-01",
        "Faithfulness metrics for retrieval-augmented generation",
        "Faithfulness measures whether every claim in a generated answer is supported by the "
        "retrieved context. It is usually computed by extracting claims from the answer and "
        "checking each one against the source passages. Low faithfulness indicates hallucinated "
        "or unsupported statements.",
        "https://example.org/rag/faithfulness",
    ),
    Document(
        "rag-02",
        "Context precision and context recall",
        "Context precision scores how many retrieved passages are actually relevant to the "
        "question, while context recall scores whether the passages contain all the information "
        "needed for a correct answer. Together they isolate retrieval quality from generation "
        "quality.",
        "https://example.org/rag/context-metrics",
    ),
    Document(
        "rag-03",
        "LLM-as-a-judge for RAG evaluation",
        "LLM-as-a-judge uses a strong language model to grade answers for correctness, relevance "
        "and groundedness. It scales better than human review but can be biased toward longer "
        "answers and its own writing style, so judge prompts should be calibrated against human "
        "labels.",
        "https://example.org/rag/llm-judge",
    ),
    Document(
        "rag-04",
        "Reference-based answer correctness",
        "When a gold answer exists, answer correctness can be measured with exact match, token "
        "F1 or semantic similarity between the generated and reference answers. Reference-based "
        "metrics are cheap and deterministic but require a labelled evaluation set.",
        "https://example.org/rag/reference-metrics",
    ),
    Document(
        "rag-05",
        "Building regression test sets from production traces",
        "Production failures are a valuable source of evaluation cases. Capturing the question, "
        "retrieved context and final answer from a failing trace lets teams turn incidents into "
        "regression tests that run before every prompt or model change.",
        "https://example.org/rag/trace-to-test",
    ),
    Document(
        "rag-06",
        "Retrieval evaluation with hit rate and MRR",
        "Classic information-retrieval metrics such as hit rate, mean reciprocal rank and nDCG "
        "evaluate whether relevant documents are ranked near the top. They require relevance "
        "judgements for each query but are fast to compute at scale.",
        "https://example.org/rag/ir-metrics",
    ),
    Document(
        "rag-07",
        "End-to-end versus component-wise RAG evaluation",
        "End-to-end evaluation scores only the final answer, which hides whether failures come "
        "from retrieval or generation. Component-wise evaluation scores the retriever and the "
        "generator separately, making root-cause analysis much easier.",
        "https://example.org/rag/component-eval",
    ),
    Document(
        "rag-08",
        "Latency and cost as evaluation dimensions",
        "A RAG system that is accurate but slow or expensive may still be unacceptable. "
        "Evaluation reports should track p95 latency, token usage and estimated cost per request "
        "alongside quality scores so trade-offs are visible.",
        "https://example.org/rag/latency-cost",
    ),
    Document(
        "rag-09",
        "Synthetic question generation for evaluation sets",
        "Synthetic evaluation sets are created by asking a language model to write questions "
        "from source documents. They bootstrap coverage quickly but should be filtered for "
        "ambiguity and validated against real user questions.",
        "https://example.org/rag/synthetic-qa",
    ),
    Document(
        "rag-10",
        "Groundedness checks with natural language inference",
        "Natural language inference models can classify whether a passage entails, contradicts "
        "or is neutral toward a generated sentence. Running NLI over answer sentences gives a "
        "deterministic groundedness signal without an LLM judge.",
        "https://example.org/rag/nli-groundedness",
    ),
    Document(
        "off-01",
        "How to make sourdough bread",
        "Sourdough relies on a fermented starter of flour and water. A long cold proof develops "
        "flavour, and baking in a preheated Dutch oven produces a crisp crust.",
        "https://example.org/cooking/sourdough",
    ),
    Document(
        "off-02",
        "History of the marathon",
        "The marathon commemorates the legendary run from Marathon to Athens. The distance was "
        "standardised at 42.195 kilometres after the 1908 London Olympics.",
        "https://example.org/sports/marathon",
    ),
    Document(
        "off-03",
        "Caring for indoor succulents",
        "Succulents need bright light, fast-draining soil and infrequent watering. Overwatering "
        "is the most common cause of root rot.",
        "https://example.org/garden/succulents",
    ),
    Document(
        "off-04",
        "Basics of chess openings",
        "Good chess openings control the centre, develop minor pieces quickly and castle early "
        "to protect the king.",
        "https://example.org/games/chess",
    ),
)
