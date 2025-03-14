from openai import AzureOpenAI
from SPARQLWrapper import SPARQLWrapper, JSON
from tokens import azure_openai_endpoint, azure_openai_key, openai_api_key
from pydantic import BaseModel
import json
from datasets import Dataset
from ragas import evaluate
from ragas.metrics import (
    faithfulness,
    answer_relevancy,
    context_recall,
    context_precision,
)
from tabulate import tabulate
import os
import time

azure_client = AzureOpenAI(azure_endpoint=azure_openai_endpoint, api_key=azure_openai_key, api_version="2025-01-01-preview")
os.environ["OPENAI_API_KEY"] = openai_api_key

ontology = """@prefix : <http://example.org/business#> .
@prefix schema: <http://schema.org/> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

schema:Corporation a rdfs:Class ;
    rdfs:subClassOf schema:Organization ;
    rdfs:label "Corporation" ;
    rdfs:comment "A company or organization engaged in commercial activities." .

schema:Person a rdfs:Class ;
    rdfs:label "Person" ;
    rdfs:comment "An individual who works in the business ecosystem." .

:BusinessSector a rdfs:Class ;
    rdfs:label "Business Sector" ;
    rdfs:comment "An industry or field in which companies operate." .

:businessRelation a rdfs:Property ;
    rdfs:label "Business Relationship" ;
    rdfs:comment "A connection between two business entities such as client, supplier, rival, etc." .

schema:name a rdf:Property ;
    rdfs:domain schema:Corporation, schema:Person;
    rdfs:range xsd:string ;
    rdfs:label "Company Name" ;
    rdfs:comment "The name of the company." .

schema:address a rdf:Property ;
    rdfs:domain schema:Corporation ;
    rdfs:range xsd:string ;
    rdfs:label "Company Address" ;
    rdfs:comment "The physical address of the company." .

schema:numberOfEmployees a rdf:Property ;
    rdfs:domain schema:Corporation ;
    rdfs:range xsd:integer ;
    rdfs:label "Number of Employees" ;
    rdfs:comment "The total number of employees in the company." .

:industry a rdf:Property ;
    rdfs:domain schema:Corporation ;
    rdfs:range :BusinessSector ;
    rdfs:label "Industry Sector" ;
    rdfs:comment "The industry in which the company operates." .

schema:worksFor a rdf:Property ;
    rdfs:domain schema:Person ;
    rdfs:range schema:Corporation ;
    rdfs:label "Works For" ;
    rdfs:comment "Indicates the company where a person is employed." .

schema:jobTitle a rdf:Property ;
    rdfs:domain schema:Person ;
    rdfs:range xsd:string ;
    rdfs:label "Job Title" ;
    rdfs:comment "The job title of a person in the company." .

:age a rdf:Property ;
    rdfs:domain schema:Person ;
    rdfs:range xsd:integer ;
    rdfs:label "Age" ;
    rdfs:comment "The age of the person." .

:client a rdf:Property ;
    rdfs:subPropertyOf :BusinessRelation ;
    rdfs:domain schema:Corporation ;
    rdfs:range schema:Corporation ;
    rdfs:label "Client" ;
    rdfs:comment "Indicates that one company is a client of another." .

:supplier a rdf:Property ;
    rdfs:subPropertyOf :BusinessRelation ;
    rdfs:domain schema:Corporation ;
    rdfs:range schema:Corporation ;
    rdfs:label "Supplier" ;
    rdfs:comment "Indicates that one company supplies goods/services to another." .

:rival a rdf:Property ;
    rdfs:subPropertyOf :BusinessRelation ;
    rdfs:domain schema:Corporation ;
    rdfs:range schema:Corporation ;
    rdfs:label "Rival" ;
    rdfs:comment "Indicates competition between two companies." .

:partner a rdf:Property ;
    rdfs:subPropertyOf :BusinessRelation ;
    rdfs:domain schema:Corporation ;
    rdfs:range schema:Corporation ;
    rdfs:label "Partner" ;
    rdfs:comment "Indicates a business partnership between two companies." .
"""

templates_descriptions = [{"id": 1, "template_description": "For the resource @node, knowing that it has a data property @predicate, return the value(s) of that data property.", "example": "Return the salary of :Jane"},
                        {"id": 2, "template_description": "For the value @value, knowing that it occurs as value of the data property @predicate, return the subject)(s) for which @predicate has the value @value.", "example": "Return those having a salary of 4000"},
                        {"id": 3, "template_description": "For the resources @node1 and @node2, return the object property(ies) by which they are directly connected, regardless of the direction of the object property.", "example": "By which relationship are :Jane and :John connected?"},
                        {"id": 4, "template_description": "For the resource @node, return all properties of it together with their values, regardless if they are literals or objects.", "example": "What attributes and relationships does :Jane have?"},
                        {"id": 5, "template_description": "For the resource @x, return all triples that involve that resource, regardless of the position it has in the triple.", "example": "Return all statements that involve :Jane"},
                        {"id": 6, "template_description": "For the resource @node, return all nodes related to it through the object property @relationship, regardless of the direction of the object property.", "example": "Return all relatives of :Jane"},
                        {"id": 7, "template_description": "For the resource @node, is there a property @predicate available, regardless of its value?", "example": "Does :Jane have a salary declared in the graph, regardless of its value?"}]

templates_patterns = [{"id": 1, "pattern": """SELECT ?val WHERE {@node @predicate ?val.}"""},
                   {"id": 2, "pattern": """SELECT ?s WHERE {?s @predicate @value.}"""},
                   {"id": 3, "pattern": """SELECT ?prop WHERE { {@node1 ?prop @node2.} UNION {@node2 ?prop @node1.} }"""},
                   {"id": 4, "pattern": """SELECT ?prop ?val WHERE {@node ?prop ?val.}"""},
                   {"id": 5, "pattern": """SELECT ?sub ?prop ?val WHERE {?sub ?prop ?val. FILTER (@x=?sub || @x=?prop || @x=?val)}"""},
                   {"id": 6, "pattern": """SELECT ?ent WHERE { @node @relationship |^ @relationship ?ent. }"""},
                   {"id": 7, "pattern": """ASK WHERE { @node @predicate ?val }"""}]

class PromptTemplateChoice(BaseModel):
    id: int

class SparqlQuery(BaseModel):
    query: str

def choose_pattern(user_natural_language_query):
    chat_messages = [
        {"role": "system", "content": f"""You are an expert in working with RDF knowledge graphs and have extensive knowledge in using SPARQL queries to retrieve and manipulate data from different knowledge graph endpoints. You specialize in identifying the most appropriate SPARQL query template based on user requests.
        Task:

        1. Understanding Inputs:
        You receive a list of dictionaries, each containing a template description for a SPARQL query pattern.
        Each template is accompanied by practical examples demonstrating its use case.
        You also receive a user query requesting a specific type of SPARQL query.

        2. Reasoning Process:
        Step 1: Analyze the user query to determine its intent and required query structure.
        Step 2: Compare the query intent with the available template descriptions and examples.
        Step 3: Identify the most appropriate template that aligns with the user query in terms of data retrieval needs, filters, and query logic.
        Step 4: Extract and return the 'id' of the chosen template.

        3.Response Format:
        Output only the 'id' of the selected template without any additional text, comments, or explanations.

        Provided Template List:
        {templates_descriptions}"""},
        {"role": "user", "content": user_natural_language_query}
    ]

    response = azure_client.beta.chat.completions.parse(
        model = "gpt-4o",
        messages = chat_messages,
        temperature = 0,
        response_format = PromptTemplateChoice
    )

    template_choice = response.choices[0].message.parsed
    template_choice_id = template_choice.id

    with open("./logs/templates_flow_logs_10_03.txt", "a") as f:
        f.write("######################################\n")
        f.write("LOGS START NEW ENTRY\n")
        f.write(user_natural_language_query + "\n")
        f.write(str(template_choice_id) + "\n")

    return template_choice_id

def generate_sparql_query(template, user_natural_language_query, rdf_jargon_prompt):
    chat_messages = [
        {"role": "system", "content": f"""You are an expert in generating SPARQL queries optimized for querying knowledge graphs. You specialize in adapting predefined SPARQL query templates to match user-provided natural language queries while strictly adhering to a given ontology.
        Task:

        1. Understanding Inputs:

        You receive:
        A SPARQL query template that serves as a base structure
        An RDF jargon description that provides a formal definition of the pattern
        A knowledge graph ontology that defines the available terms and relationships
        
        2. Reasoning Process:
        Step 1: Carefully analyze the ontology to identify the appropriate classes, properties, and relationships that align with the user's intent.
        Step 2: Interpret the RDF jargon description to understand the technical constraints of the query pattern.
        Step 3: Modify the SPARQL template to ensure it correctly represents the user’s query while strictly using terms from the provided ontology. When modifying the SPARQL template do not alter its structure. Add only the necessary data from the user's prompt in the place of the placeholders from the template, starting with an @.
        Step 4: Validate that the generated SPARQL query maintains logical consistency, adheres to best practices, and accurately retrieves the intended information.
        
        3. Response Format:
        Output only the generated SPARQL query without any additional text, comments, or explanations.
        
        Provided Information:
        SPARQL Template: {template}
        RDF Jargon Description: {rdf_jargon_prompt}
        Ontology: {ontology}"""},
        {"role": "user", "content": "The natural language query is: " + user_natural_language_query}
    ]

    response = azure_client.beta.chat.completions.parse(
        model = "gpt-4o",
        messages = chat_messages,
        temperature = 0,
        response_format = SparqlQuery
    )

    generated_sparql = response.choices[0].message.parsed.query

    with open("./logs/templates_flow_logs_10_03.txt", "a") as f:
        f.write(generated_sparql + "\n")

    return generated_sparql

def process_sparql_query(sparql_query):
    endpoint = "http://localhost:7200/repositories/caise_companies"
    prefixes = """PREFIX : <http://example.org/business#>
PREFIX schema: <http://schema.org#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""
    query_to_send = prefixes + sparql_query
    sparql = SPARQLWrapper(endpoint)
    sparql.setQuery(query_to_send)
    sparql.setReturnFormat(JSON)
    results = sparql.queryAndConvert()

    with open("./logs/templates_flow_logs_10_03.txt", "a") as f:
        f.write(str(results) + "\n")

    return results


def convert_sparql_query_results_to_text(sparql_query_result, natural_language_user_query):
    chat_messages = [
        {"role": "system", "content": f"""You are an expert in transforming RDF knowledge graph structures, provided in JSON format, into clear and easily readable natural language text. Your task is to analyze the RDF structure and generate a human-readable summary that preserves all entities, relationships, and data while maintaining coherence.
        Task:

        1. Understanding Inputs:
        You receive an RDF knowledge graph structure in JSON format. The JSON structure consists of subjects (entities), predicates (relationships), and objects (values or linked entities).
        You receive a user query in natural language.
        
        2. Reasoning Process:
        Step 1: Extract all entities (subjects) and identify their corresponding properties and relationships.
        Step 2: Analyze the predicates to determine the connections between entities.
        Step 3: Structure the extracted data into well-formed, natural language sentences that clearly describe the relationships and attributes.
        Step 4: Ensure readability by organizing the information logically and avoiding redundancy.
        
        3. Response Format:
        Formulate the natural language text in a way that clearly indicates it is the direct response to the user's query.
        Use natural language to directly indicate that the identified entity or entities are the response. For example, if the user asks about a person, structure the response as:
        'The entity relevant to your query is [Entity], which is related to [Other Entity] through [Relationship].'
        Maintain coherence and readability, avoiding redundancy while ensuring completeness.
        Output only the transformed natural language text without any additional comments or explanations.
        
        Provided Information:
        Natural language user query: {natural_language_user_query}"""},
        {"role": "user", "content": f"""The JSON strucutre you need to transform is: {sparql_query_result}"""}
    ]
    response = azure_client.chat.completions.create(
        model = "gpt-4o",
        messages = chat_messages,
        temperature = 0,
    )
    natural_language_text = response.choices[0].message.content

    with open("./logs/templates_flow_logs_10_03.txt", "a") as f:
        f.write(natural_language_text + "\n")

    return natural_language_text


def respond_to_user(user_natural_language_query, context):
    chat_messages = [
        {"role": "system", "content": f"""You are a highly precise and concise AI assistant. Your task is to respond to user queries strictly based on the provided context while ensuring clarity and relevance.
        Task:

        1. Understanding Inputs:
        You are given a specific context
        User queries must be answered exclusively based on this context, without introducing external knowledge.
        
        2. Reasoning Process:
        Step 1: Carefully analyze the provided context to identify relevant information.
        Step 2: Determine the most accurate and concise response to the user query using only the given context.
        Step 3: Ensure that the response remains precise, avoiding unnecessary elaboration or unrelated details.
        
        3. Response Format:
        Provide only the direct answer to the user query, formatted concisely and clearly.
        Do not include additional commentary, disclaimers, or external information beyond the given context.
        
        Provided Context:
        {context}"""},
        {"role": "user", "content": f"""The user query you need to respond to is: {user_natural_language_query}"""}
    ]
    response = azure_client.chat.completions.create(
        model = "gpt-4o",
        messages = chat_messages,
        temperature = 0,
    )
    response = response.choices[0].message.content

    with open("./logs/templates_flow_logs_10_03.txt", "a") as f:
        f.write(response + "\n")
        f.write("END LOGS \n")
        f.write("######################################\n\n")

    return response

def respond_to_user_query(user_natural_language_query):
    pattern_choice = choose_pattern(user_natural_language_query)
    json_templates_descriptions = json.dumps(templates_descriptions)
    json_templates_descriptions_final = json.loads(json_templates_descriptions)
    rdf_jargon_pattern = json_templates_descriptions_final[pattern_choice-1]["template_description"]
    json_patterns = json.dumps(templates_patterns)
    json_patterns_final = json.loads(json_patterns)
    sparql_template = json_patterns_final[pattern_choice-1]["pattern"]
    print(sparql_template)
    generated_sparql_query = generate_sparql_query(sparql_template, user_natural_language_query, rdf_jargon_pattern)
    print(generated_sparql_query)
    results = process_sparql_query(generated_sparql_query)
    context = convert_sparql_query_results_to_text(results, user_natural_language_query)
    response = respond_to_user(user_natural_language_query, context)
    return context, response

def ragas_analyze():
    questions = ["Return the number of employees of :TechNova.", 
                "Return the people who are 29 years old.", 
                "What is the relationship between :TechNova and :FinTrust?",
                "What attributes and relationships does :AliceJ have?", 
                "Return all statements that involve :HealthFirst.",
                "Return all the employees of :TechNova declared in the graph.",
                "Does :RetailX have an address declared in the graph, regardless of its value?"]
    
    ground_truths = ["TechNova Inc. has 500 employees.",
                    "Clara Davis is 29 years old.",
                    "TechNova Inc. is a client of FinTrust Bank.",
                    "Alice Johnson is 42 years old. She is the CEO of TechNova Inc.",
                    "HealthFirst is a corporation in the healthcare industry. HealthFirst is located at 78 Wellness Blvd, Berlin, Germany. HealthFirst has 400 employees. HealthFirst is a partner of FinTrust Bank. HealthFirst is a client of AutoMotiveTech. Eva Wilson works at HealthFirst as the Medical Director. Isabel Foster works at HealthFirst as the Head of R&D.",
                    "The employees are Alice Johnson who works as a CEO, Bob Smith who works as a Software Engineer and Jack Green who works as a CTO.",
                    "Yes."]
    answers = []
    contexts = []
    for question in questions:
        context_list = []
        context, answer = respond_to_user_query(question)
        context_list.append(context) 
        contexts.append(context_list)
        answers.append(answer)
        time.sleep(10)
    
    data = {
        "question": questions,
        "answer": answers,
        "contexts": contexts,
        "ground_truths": ground_truths,
        "reference": ground_truths   
    }
    dataset = Dataset.from_dict(data)
    result = evaluate(
        dataset = dataset,
        metrics = [
            context_precision,
            context_recall,
            faithfulness,
            answer_relevancy
        ],
    )

    df = result.to_pandas()
    df.to_csv("./ragas_results/10_03_res/results_templates_flow_10_03_v5.csv", index=False)

def main():
    ragas_analyze()

main()