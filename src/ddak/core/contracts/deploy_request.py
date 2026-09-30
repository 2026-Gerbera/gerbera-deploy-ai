"""배포 요청(DeployRequest). receive_deploy_request의 입력.

채팅의 의도 JSON(AI, 예: {"action": "deploy_request", "target": "both"})을 코드가 enum·ref 존재로
확인한 뒤 이 모델로 바꾼다. 부작용은 항상 사람의 [배포] 클릭(승인) 뒤에만 일어난다(✅ 장부 4).

TODO(contract): 필드는 docs/contracts/ 계약 문서가 정한다. 하네스는 자리만 둔다.
"""
