"""
API路由模块
"""

from flask import Blueprint, request, jsonify, render_template, redirect, url_for
from backend.services.interview_service import RoomService, SessionService, RoundService
from backend.utils.minio_client import download_resume_data, minio_client

# === digitalhub 客户端 & os ===
from backend.services.digitalhub_client import ping_dh, boot_dh, start_llm
import os

# 创建蓝图
main_bp = Blueprint('main', __name__)
api_bp = Blueprint('api', __name__, url_prefix='/api')


# 主页面路由
@main_bp.route('/')
def index():
    """首页 - 显示面试间列表和系统统计"""
    rooms = RoomService.get_all_rooms()
    rooms_dict = [RoomService.to_dict(room) for room in rooms]
    
    # 计算系统统计数据
    total_sessions = 0
    total_rounds = 0
    total_questions = 0
    
    for room in rooms:
        sessions = SessionService.get_sessions_by_room(room.id)
        total_sessions += len(sessions)
        
        for session in sessions:
            rounds = RoundService.get_rounds_by_session(session.id)
            total_rounds += len(rounds)
            
            for round_obj in rounds:
                total_questions += round_obj.questions_count
    
    # 系统统计数据
    stats = {
        'total_rooms': len(rooms),
        'total_sessions': total_sessions,
        'total_rounds': total_rounds,
        'total_questions': total_questions
    }
    
    return render_template('index.html', rooms=rooms_dict, stats=stats)


@main_bp.route('/create_room')
def create_room():
    """创建新的面试间"""
    # 进入创建面试间时，静默 ping 数字人
    try:
        ping_dh()
    except Exception:
        pass

    room = RoomService.create_room()
    return redirect(url_for('main.room_detail', room_id=room.id))


@main_bp.route('/room/<room_id>')
def room_detail(room_id):
    """面试间详情页面"""
    # 点击已有面试间时，静默 ping 数字人
    try:
        ping_dh()
    except Exception:
        pass

    room = RoomService.get_room(room_id)
    if not room:
        return "面试间不存在", 404
    
    sessions = SessionService.get_sessions_by_room(room_id)
    sessions_dict = [SessionService.to_dict(session) for session in sessions]
    
    return render_template('room.html', 
                         room=RoomService.to_dict(room), 
                         sessions=sessions_dict)


@main_bp.route('/create_session/<room_id>')
def create_session(room_id):
    """在指定面试间创建新的面试会话"""
    session = SessionService.create_session(room_id)
    if not session:
        return "面试间不存在", 404

    # 变更：创建会话后，不再跳数字人；跳回会话页，由会话页触发启动并展示链接
    return redirect(url_for('main.session_detail', session_id=session.id))


@main_bp.route('/session/<session_id>')
def session_detail(session_id):
    """面试会话详情页面（进入此页时启动数字人，并把提示作为AI消息插入）"""
    session = SessionService.get_session(session_id)
    if not session:
        return "面试会话不存在", 404

    # 进入会话页时，启动数字人（已在运行则复用），拿到提示文案与链接
    dh_message, dh_connect_url = None, None
    try:
        public_host = os.getenv("PUBLIC_HOST")
        resp = boot_dh(session.room_id, session.id, public_host=public_host)
        dh_message = (resp.get("data") or {}).get("message")
        dh_connect_url = (resp.get("data") or {}).get("connect_url")
    except Exception as e:
        # 启动失败不阻塞页面；前端只是不显示那条提示
        dh_message, dh_connect_url = None, None

    rounds = RoundService.get_rounds_by_session(session_id)
    rounds_dict = []
    
    # 为每个round加载问题数据
    for round_obj in rounds:
        round_data = RoundService.to_dict(round_obj)
        
        # 尝试从MinIO加载问题数据
        try:
            # 从questions_file_path提取文件标识
            file_path = round_data['questions_file_path']
            if 'questions_round_' in file_path:
                # 提取round标识符，如"0_session_id"格式
                round_identifier = file_path.split('questions_round_')[1].split('.json')[0]
                questions_data = minio_client.download_json(f"data/questions_round_{round_identifier}.json")
                if questions_data:
                    round_data['questions'] = questions_data.get('questions', [])
                else:
                    round_data['questions'] = []
            else:
                round_data['questions'] = []
        except Exception as e:
            print(f"Error loading questions for round {round_data['id']}: {e}")
            round_data['questions'] = []
        
        rounds_dict.append(round_data)
    
    # 获取简历数据用于展示
    resume_data = download_resume_data()
    
    return render_template('session.html',
                         session=SessionService.to_dict(session),
                         rounds=rounds_dict,
                         resume=resume_data,
                         # 新增：注入数字人提示与链接，供模板插入AI消息/按钮
                         dh_message=dh_message,
                         dh_connect_url=dh_connect_url)


@main_bp.route('/generate_questions/<session_id>', methods=['POST'])
def generate_questions(session_id):
    """生成面试题 + 启动 LLM Round Server"""
    session = SessionService.get_session(session_id)
    if not session:
        return jsonify({'error': '面试会话不存在'}), 404

    try:
        from backend.services.question_service import get_question_generation_service
        service = get_question_generation_service()
        result = service.generate_questions(session_id)

        if result['success']:
            # 题目生成成功后，启动 LLM（按约定用 session_id/round_index）
            try:
                llm_info = start_llm(
                    session_id=session_id,
                    round_index=int(result.get('round_index', 0)),
                    port=int(os.getenv("LLM_PORT", "8011")),
                    minio_endpoint=os.getenv("MINIO_ENDPOINT", "test-minio.yeying.pub"),
                    minio_access_key=os.getenv("MINIO_ACCESS_KEY", ""),
                    minio_secret_key=os.getenv("MINIO_SECRET_KEY", ""),
                    minio_bucket=os.getenv("MINIO_BUCKET", "yeying-interviewer"),
                    minio_secure=os.getenv("MINIO_SECURE", "true").lower() == "true",
                )
                result['llm'] = llm_info.get('data', llm_info)
            except Exception as e:
                result['llm_error'] = str(e)

            return jsonify(result)
        else:
            return jsonify({'error': result['error']}), 500
    except Exception as e:
        return jsonify({'error': f'生成面试题失败: {str(e)}'}), 500


@main_bp.route('/get_current_question/<round_id>')
def get_current_question(round_id):
    """获取当前问题"""
    try:
        from backend.services.question_service import get_question_generation_service
        service = get_question_generation_service()
        question_data = service.get_current_question(round_id)

        if question_data:
            return jsonify({
                'success': True,
                'question_data': question_data
            })
        else:
            return jsonify({
                'success': False,
                'message': '没有更多问题了'
            })
    except Exception as e:
        return jsonify({'error': f'获取问题失败: {str(e)}'}), 500


@main_bp.route('/save_answer', methods=['POST'])
def save_answer():
    """保存用户回答"""
    try:
        data = request.get_json()
        qa_id = data.get('qa_id')
        answer_text = data.get('answer_text')

        if not qa_id or not answer_text:
            return jsonify({'error': '缺少必要参数'}), 400

        from backend.services.question_service import get_question_generation_service
        service = get_question_generation_service()
        result = service.save_answer(qa_id, answer_text.strip())

        return jsonify(result)

    except Exception as e:
        return jsonify({'error': f'保存回答失败: {str(e)}'}), 500


@main_bp.route('/get_qa_analysis/<session_id>/<int:round_index>')
def get_qa_analysis(session_id, round_index):
    """获取指定轮次的QA分析数据"""
    try:
        from backend.utils.minio_client import minio_client

        # 尝试从MinIO加载分析数据
        analysis_filename = f"analysis/qa_complete_{round_index}_{session_id}.json"
        analysis_data = minio_client.download_json(analysis_filename)

        if analysis_data:
            return jsonify({
                'success': True,
                'analysis_data': analysis_data,
                'file_path': analysis_filename
            })
        else:
            return jsonify({
                'success': False,
                'message': '分析数据不存在或轮次未完成'
            }), 404

    except Exception as e:
        return jsonify({'error': f'获取分析数据失败: {str(e)}'}), 500


# API路由
@api_bp.route('/rooms')
def api_rooms():
    """API: 获取所有面试间"""
    rooms = RoomService.get_all_rooms()
    return jsonify([RoomService.to_dict(room) for room in rooms])


@api_bp.route('/rooms/<room_id>', methods=['DELETE'])
def api_delete_room(room_id):
    """API: 删除面试间"""
    try:
        success = RoomService.delete_room(room_id)
        if success:
            return jsonify({'success': True, 'message': '面试间删除成功'})
        else:
            return jsonify({'success': False, 'error': '面试间不存在'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': f'删除失败: {str(e)}'}), 500


@api_bp.route('/sessions/<session_id>', methods=['DELETE'])
def api_delete_session(session_id):
    """API: 删除面试会话"""
    try:
        success = SessionService.delete_session(session_id)
        if success:
            return jsonify({'success': True, 'message': '面试会话删除成功'})
        else:
            return jsonify({'success': False, 'error': '面试会话不存在'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': f'删除失败: {str(e)}'}), 500


@api_bp.route('/sessions/<room_id>')
def api_sessions(room_id):
    """API: 获取指定面试间的所有会话"""
    sessions = SessionService.get_sessions_by_room(room_id)
    return jsonify([SessionService.to_dict(session) for session in sessions])


@api_bp.route('/rounds/<session_id>')
def api_rounds(session_id):
    """API: 获取指定会话的所有轮次"""
    rounds = RoundService.get_rounds_by_session(session_id)
    return jsonify([RoundService.to_dict(round_obj) for round_obj in rounds])


@api_bp.route('/minio/test')
def api_minio_test():
    """API: 测试MinIO连接和数据访问"""
    try:
        # 测试列出文件
        objects = minio_client.list_objects(prefix="data/")
        
        # 测试加载简历数据
        resume_data = download_resume_data()
        
        return jsonify({
            'status': 'success',
            'minio_objects': objects,
            'resume_loaded': resume_data is not None,
            'candidate_name': resume_data.get('name') if resume_data else None
        })
        
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500
