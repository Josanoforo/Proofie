"""Separate stdio MCP server, serving local synthetic/recorded registry only."""
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from copilot.features.mcp_local_contracts import service
class AuditedLocalMCPServer(MCPServer):
 async def list_tools(self):
  tools=await super().list_tools()
  for tool in tools:
   tool.input_schema['additionalProperties']=False
  return tools
 async def call_tool(self,name,arguments,context=None):
  if name not in service.TOOLS:
   service.audit_protocol_refusal(name,arguments)
   raise ToolError('Tool is not allowed by the local server policy.')
  expected={'list_local_cases':set(),'read_local_case':{'case_id'},'prepare_local_note':{'case_id','note'}}[name]
  valid=isinstance(arguments,dict) and set(arguments)==expected
  if valid and name=='read_local_case':
   valid=isinstance(arguments['case_id'],str) and 0<len(arguments['case_id'])<=100
  if valid and name=='prepare_local_note':
   valid=isinstance(arguments['case_id'],str) and 0<len(arguments['case_id'])<=100 and isinstance(arguments['note'],str) and bool(arguments['note'].strip()) and len(arguments['note'])<=1000
  if not valid:
   service.audit_protocol_refusal(name,arguments)
   raise ToolError('Arguments do not match the strict local tool schema.')
  return await super().call_tool(name,arguments,context)

mcp=AuditedLocalMCPServer('proofie-local-contracts',instructions='Local cases only. No PayPal, send, approve, or accept-claim tools.')
@mcp.tool()
def list_local_cases() -> list[dict]:
 try:return service.invoke('list_local_cases',{},actor=service.os.environ.get('PROOFIE_MCP_LOCAL_ACTOR','local-user'))
 except (PermissionError,ValueError) as exc:raise ToolError(str(exc)) from exc
@mcp.tool()
def read_local_case(case_id:str) -> dict:
 try:return service.invoke('read_local_case',{'case_id':case_id},actor=service.os.environ.get('PROOFIE_MCP_LOCAL_ACTOR','local-user'))
 except (PermissionError,ValueError) as exc:raise ToolError(str(exc)) from exc
@mcp.tool()
def prepare_local_note(case_id:str,note:str) -> dict:
 try:return service.invoke('prepare_local_note',{'case_id':case_id,'note':note},actor=service.os.environ.get('PROOFIE_MCP_LOCAL_ACTOR','local-user'))
 except (PermissionError,ValueError) as exc:raise ToolError(str(exc)) from exc
def main():
 from copilot import config
 config.load_env_file()
 if not config.feature_enabled('mcp_local_contracts'):
  raise SystemExit('Local MCP contracts are disabled; set COPILOT_FEATURE_MCP_LOCAL_CONTRACTS=true to start this server.')
 mcp.run(transport='stdio')
if __name__=='__main__':main()
